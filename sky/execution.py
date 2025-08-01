"""Execution layer.

See `Stage` for a Task's life cycle.
"""
import enum
import typing
from typing import List, Optional, Tuple, Union

import colorama

from sky import admin_policy
from sky import backends
from sky import clouds
from sky import global_user_state
from sky import optimizer
from sky import sky_logging
from sky.backends import backend_utils
from sky.skylet import autostop_lib
from sky.usage import usage_lib
from sky.utils import admin_policy_utils
from sky.utils import common
from sky.utils import controller_utils
from sky.utils import dag_utils
from sky.utils import resources_utils
from sky.utils import rich_utils
from sky.utils import status_lib
from sky.utils import tempstore
from sky.utils import timeline
from sky.utils import ux_utils

if typing.TYPE_CHECKING:
    import sky

logger = sky_logging.init_logger(__name__)


class Stage(enum.Enum):
    """Stages for a run of a sky.Task."""
    # TODO: rename actual methods to be consistent.
    CLONE_DISK = enum.auto()
    OPTIMIZE = enum.auto()
    PROVISION = enum.auto()
    SYNC_WORKDIR = enum.auto()
    SYNC_FILE_MOUNTS = enum.auto()
    SETUP = enum.auto()
    PRE_EXEC = enum.auto()
    EXEC = enum.auto()
    DOWN = enum.auto()


def _maybe_clone_disk_from_cluster(clone_disk_from: Optional[str],
                                   cluster_name: Optional[str],
                                   task: 'sky.Task') -> 'sky.Task':
    if clone_disk_from is None:
        return task
    task, handle = backend_utils.check_can_clone_disk_and_override_task(
        clone_disk_from, cluster_name, task)
    original_cloud = handle.launched_resources.cloud
    assert original_cloud is not None, handle.launched_resources
    task_resources = list(task.resources)[0]

    with rich_utils.safe_status('Creating image from source cluster '
                                f'{clone_disk_from!r}'):
        image_id = original_cloud.create_image_from_cluster(
            cluster_name=resources_utils.ClusterName(
                display_name=clone_disk_from,
                name_on_cloud=handle.cluster_name_on_cloud),
            region=handle.launched_resources.region,
            zone=handle.launched_resources.zone,
        )
        rich_utils.force_update_status(
            f'Migrating image {image_id} to target region '
            f'{task_resources.region}...')
        source_region = handle.launched_resources.region
        target_region = task_resources.region
        assert source_region is not None, handle.launched_resources
        assert target_region is not None, task_resources

        image_id = original_cloud.maybe_move_image(
            image_id,
            source_region=source_region,
            target_region=target_region,
            source_zone=handle.launched_resources.zone,
            target_zone=task_resources.zone,
        )
    logger.info(
        f'{colorama.Fore.GREEN}'
        f'Successfully created image {image_id!r} for {clone_disk_from!r} '
        f'on {original_cloud}.{colorama.Style.RESET_ALL}\n'
        'Overriding task\'s image_id.')
    task_resources = task_resources.copy(image_id=image_id,
                                         _is_image_managed=True)
    task.set_resources(task_resources)
    # Set the best_resources to None to trigger a re-optimization, so that
    # the new task_resources is used.
    task.best_resources = None
    logger.debug(f'Overridden task resources: {task.resources}')
    return task


def _execute(
    entrypoint: Union['sky.Task', 'sky.Dag'],
    dryrun: bool = False,
    down: bool = False,
    stream_logs: bool = True,
    handle: Optional[backends.ResourceHandle] = None,
    backend: Optional[backends.Backend] = None,
    retry_until_up: bool = False,
    optimize_target: common.OptimizeTarget = common.OptimizeTarget.COST,
    stages: Optional[List[Stage]] = None,
    cluster_name: Optional[str] = None,
    detach_setup: bool = False,
    detach_run: bool = False,
    idle_minutes_to_autostop: Optional[int] = None,
    no_setup: bool = False,
    clone_disk_from: Optional[str] = None,
    skip_unnecessary_provisioning: bool = False,
    # Internal only:
    # pylint: disable=invalid-name
    _quiet_optimizer: bool = False,
    _is_launched_by_jobs_controller: bool = False,
    _is_launched_by_sky_serve_controller: bool = False,
) -> Tuple[Optional[int], Optional[backends.ResourceHandle]]:
    """Execute an entrypoint.

    If sky.Task is given or DAG has not been optimized yet, this will call
    sky.optimize() for the caller.

    Args:
      entrypoint: sky.Task or sky.Dag.
      dryrun: bool; if True, only print the provision info (e.g., cluster
        yaml).
      down: bool; whether to tear down the launched resources after all jobs
        finish (successfully or abnormally). If idle_minutes_to_autostop is
        also set, the cluster will be torn down after the specified idle time.
        Note that if errors occur during provisioning/data syncing/setting up,
        the cluster will not be torn down for debugging purposes.
      stream_logs: bool; whether to stream all tasks' outputs to the client.
      handle: Optional[backends.ResourceHandle]; if provided, execution will
        attempt to use an existing backend cluster handle instead of
        provisioning a new one.
      backend: Backend; backend to use for executing the tasks. Defaults to
        CloudVmRayBackend()
      retry_until_up: bool; whether to retry the provisioning until the cluster
        is up.
      optimize_target: OptimizeTarget; the dag optimization metric, e.g.
        OptimizeTarget.COST.
      stages: List of stages to run.  If None, run the whole life cycle of
        execution; otherwise, just the specified stages.  Used for `sky exec`
        skipping all setup steps.
      cluster_name: Name of the cluster to create/reuse.  If None,
        auto-generate a name.
      detach_setup: If True, run setup in non-interactive mode as part of the
        job itself. You can safely ctrl-c to detach from logging, and it will
        not interrupt the setup process. To see the logs again after detaching,
        use `sky logs`. To cancel setup, cancel the job via `sky cancel`.
      detach_run: If True, as soon as a job is submitted, return from this
        function and do not stream execution logs.
      idle_minutes_to_autostop: int; if provided, the cluster will be set to
        autostop after this many minutes of idleness.
      no_setup: bool; whether to skip setup commands or not when (re-)launching.
      clone_disk_from: Optional[str]; if set, clone the disk from the specified
        cluster.
      skip_unnecessary_provisioning: bool; if True, compare the calculated
        cluster config to the current cluster's config. If they match, shortcut
        provisioning and setup, even if we have Stage.PROVISION and Stage.SETUP.

    Returns:
      job_id: Optional[int]; the job ID of the submitted job. None if the
        backend is not CloudVmRayBackend, or no job is submitted to
        the cluster.
      handle: Optional[backends.ResourceHandle]; the handle to the cluster. None
        if dryrun.
    """
    dag = dag_utils.convert_entrypoint_to_dag(entrypoint)
    dag.resolve_and_validate_volumes()
    if (not _is_launched_by_jobs_controller and
            not _is_launched_by_sky_serve_controller):
        # Only process pre-mount operations on API server.
        dag.pre_mount_volumes()
    for task in dag.tasks:
        if task.storage_mounts is not None:
            for storage in task.storage_mounts.values():
                # Ensure the storage is constructed.
                storage.construct()
        for resource in task.resources:
            # For backward compatibility, we need to override the autostop
            # config at server-side for legacy clients.
            # TODO(aylei): remove this after we bump the API version.
            resource.override_autostop_config(
                down=down, idle_minutes=idle_minutes_to_autostop)
            if resource.autostop_config is not None:
                down = resource.autostop_config.down
                idle_minutes_to_autostop = resource.autostop_config.idle_minutes
    with admin_policy_utils.apply_and_use_config_in_current_request(
            dag,
            request_options=admin_policy.RequestOptions(
                cluster_name=cluster_name,
                idle_minutes_to_autostop=idle_minutes_to_autostop,
                down=down,
                dryrun=dryrun,
            )) as dag:
        return _execute_dag(
            dag,
            dryrun=dryrun,
            stream_logs=stream_logs,
            handle=handle,
            backend=backend,
            retry_until_up=retry_until_up,
            optimize_target=optimize_target,
            stages=stages,
            cluster_name=cluster_name,
            detach_setup=detach_setup,
            detach_run=detach_run,
            no_setup=no_setup,
            clone_disk_from=clone_disk_from,
            skip_unnecessary_provisioning=skip_unnecessary_provisioning,
            _quiet_optimizer=_quiet_optimizer,
            _is_launched_by_jobs_controller=_is_launched_by_jobs_controller,
            _is_launched_by_sky_serve_controller=
            _is_launched_by_sky_serve_controller)


def _execute_dag(
    dag: 'sky.Dag',
    dryrun: bool,
    stream_logs: bool,
    handle: Optional[backends.ResourceHandle],
    backend: Optional[backends.Backend],
    retry_until_up: bool,
    optimize_target: common.OptimizeTarget,
    stages: Optional[List[Stage]],
    cluster_name: Optional[str],
    detach_setup: bool,
    detach_run: bool,
    no_setup: bool,
    clone_disk_from: Optional[str],
    skip_unnecessary_provisioning: bool,
    # pylint: disable=invalid-name
    _quiet_optimizer: bool,
    _is_launched_by_jobs_controller: bool,
    _is_launched_by_sky_serve_controller: bool,
) -> Tuple[Optional[int], Optional[backends.ResourceHandle]]:
    """Execute a DAG.

    This is an internal helper function for _execute() and is expected to be
    called only by _execute().
    """
    assert len(dag) == 1, f'We support 1 task for now. {dag}'
    task = dag.tasks[0]

    if any(r.job_recovery is not None for r in task.resources):
        logger.warning(
            f'{colorama.Style.DIM}The task has `job_recovery` specified, '
            'but is launched as an unmanaged job. It will be ignored.'
            'To enable job recovery, use managed jobs: sky jobs launch.'
            f'{colorama.Style.RESET_ALL}')

    cluster_exists = False
    if cluster_name is not None:
        cluster_record = global_user_state.get_cluster_from_name(cluster_name)
        cluster_exists = cluster_record is not None
        # TODO(woosuk): If the cluster exists, print a warning that
        # `cpus` and `memory` are not used as a job scheduling constraint,
        # unlike `gpus`.

    stages = stages if stages is not None else list(Stage)

    # Requested features that some clouds support and others don't.
    requested_features = set()

    is_controller_high_availability_supported = False

    controller = controller_utils.Controllers.from_name(cluster_name)
    if controller is not None:
        requested_features.add(
            clouds.CloudImplementationFeatures.HOST_CONTROLLERS)
        if controller_utils.high_availability_specified(cluster_name):
            requested_features.add(clouds.CloudImplementationFeatures.
                                   HIGH_AVAILABILITY_CONTROLLERS)
            # If we provision a cluster that supports high availability
            # controllers, we can use the high availability controller.
            is_controller_high_availability_supported = True

    # Add requested features from the task
    requested_features |= task.get_required_cloud_features()

    backend = backend if backend is not None else backends.CloudVmRayBackend()
    # Figure out autostop config.
    # Note: Ideally this can happen after provisioning, so we can check the
    # autostop config from the launched resources. Before provisioning,
    # we aren't sure which resources will be launched, and different
    # resources may have different autostop configs.
    if isinstance(backend, backends.CloudVmRayBackend):
        # No autostop config specified on command line, use the
        # config from resources.
        # TODO(cooperc): This should be done after provisioning, in order to
        # support different autostop configs for different resources.
        # Blockers:
        # - Need autostop config to set requested_features before
        #   provisioning.
        # - Need to send info message about idle_minutes_to_autostop==0 here
        # - Need to check if autostop is supported by the backend.
        resources = list(task.resources)
        for resource in resources:
            if resource.autostop_config != resources[0].autostop_config:
                raise ValueError(
                    'All resources must have the same autostop config.')
        resource_autostop_config = resources[0].autostop_config

        idle_minutes_to_autostop: Optional[int] = None
        down = False
        wait_for: Optional[autostop_lib.AutostopWaitFor] = None
        if resource_autostop_config is not None:
            if resource_autostop_config.enabled:
                idle_minutes_to_autostop = (
                    resource_autostop_config.idle_minutes)
                down = resource_autostop_config.down
                wait_for = resource_autostop_config.wait_for
            else:
                # Autostop is explicitly disabled, so cancel it if it's
                # already set.
                assert not resource_autostop_config.enabled
                idle_minutes_to_autostop = -1
                down = False
        if idle_minutes_to_autostop is not None:
            if idle_minutes_to_autostop == 0:
                # idle_minutes_to_autostop=0 can cause the following problem:
                # After we set the autostop in the PRE_EXEC stage with -i 0,
                # it could be possible that the cluster immediately found
                # itself have no task running and start the auto{stop,down}
                # process, before the task is submitted in the EXEC stage.
                verb = 'torn down' if down else 'stopped'
                logger.info(f'{colorama.Style.DIM}The cluster will '
                            f'be {verb} after 1 minutes of idleness '
                            '(after all jobs finish).'
                            f'{colorama.Style.RESET_ALL}')
                idle_minutes_to_autostop = 1
            if Stage.DOWN in stages:
                stages.remove(Stage.DOWN)
            if idle_minutes_to_autostop >= 0:
                if down:
                    requested_features.add(
                        clouds.CloudImplementationFeatures.AUTODOWN)
                else:
                    requested_features.add(
                        clouds.CloudImplementationFeatures.AUTOSTOP)
        # NOTE: in general we may not have sufficiently specified info
        # (cloud/resource) to check STOP_SPOT_INSTANCE here. This is checked in
        # the backend.

    if Stage.CLONE_DISK in stages:
        task = _maybe_clone_disk_from_cluster(clone_disk_from, cluster_name,
                                              task)

    if not cluster_exists:
        # If spot is launched on serve or jobs controller, we don't need to
        # print out the hint.
        if (Stage.PROVISION in stages and task.use_spot and
                not _is_launched_by_jobs_controller and
                not _is_launched_by_sky_serve_controller):
            yellow = colorama.Fore.YELLOW
            bold = colorama.Style.BRIGHT
            reset = colorama.Style.RESET_ALL
            logger.info(
                f'{yellow}Launching a spot job that does not '
                f'automatically recover from preemptions. To '
                'get automatic recovery, use managed job instead: '
                f'{reset}{bold}sky jobs launch{reset} {yellow}or{reset} '
                f'{bold}sky.jobs.launch(){reset}.')

        if Stage.OPTIMIZE in stages:
            if task.best_resources is None:
                # TODO: fix this for the situation where number of requested
                # accelerators is not an integer.
                if isinstance(backend, backends.CloudVmRayBackend):
                    # TODO: adding this check because docker backend on a
                    # no-credential machine should not enter optimize(), which
                    # would directly error out ('No cloud is enabled...').  Fix
                    # by moving `sky check` checks out of optimize()?
                    controller = controller_utils.Controllers.from_name(
                        cluster_name)
                    if controller is not None:
                        logger.info(
                            f'Choosing resources for {controller.value.name}...'
                        )
                    dag = optimizer.Optimizer.optimize(dag,
                                                       minimize=optimize_target,
                                                       quiet=_quiet_optimizer)
                    task = dag.tasks[0]  # Keep: dag may have been deep-copied.
                    assert task.best_resources is not None, task

    backend.register_info(
        dag=dag,
        optimize_target=optimize_target,
        requested_features=requested_features,
        # That's because we want to do commands in task.setup and task.run again
        # after K8S pod recovers from a crash.
        # See `kubernetes-ray.yml.j2` for more details.
        dump_final_script=is_controller_high_availability_supported)

    if task.storage_mounts is not None:
        # Optimizer should eventually choose where to store bucket
        task.sync_storage_mounts()

    try:
        provisioning_skipped = False
        if Stage.PROVISION in stages:
            assert handle is None or skip_unnecessary_provisioning, (
                'Provisioning requested, but handle is already set. PROVISION '
                'should be excluded from stages or '
                'skip_unecessary_provisioning should be set. ')
            (handle, provisioning_skipped) = backend.provision(
                task,
                task.best_resources,
                dryrun=dryrun,
                stream_logs=stream_logs,
                cluster_name=cluster_name,
                retry_until_up=retry_until_up,
                skip_unnecessary_provisioning=skip_unnecessary_provisioning)

        if handle is None:
            assert dryrun, ('If not dryrun, handle must be set or '
                            'Stage.PROVISION must be included in stages.')
            logger.info('Dryrun finished.')
            return None, None

        do_workdir = (Stage.SYNC_WORKDIR in stages and not dryrun and
                      task.workdir is not None)
        do_file_mounts = (Stage.SYNC_FILE_MOUNTS in stages and not dryrun and
                          (task.file_mounts is not None or
                           task.storage_mounts is not None))
        if do_workdir or do_file_mounts:
            logger.info(ux_utils.starting_message('Syncing files.'))

        if do_workdir:
            backend.sync_workdir(handle, task.workdir, task.envs_and_secrets)

        if do_file_mounts:
            backend.sync_file_mounts(handle, task.file_mounts,
                                     task.storage_mounts)

        if no_setup:
            logger.info('Setup commands skipped.')
        elif Stage.SETUP in stages and not dryrun:
            if skip_unnecessary_provisioning and provisioning_skipped:
                logger.debug('Unnecessary provisioning was skipped, so '
                             'skipping setup as well.')
            else:
                backend.setup(handle, task, detach_setup=detach_setup)

        if Stage.PRE_EXEC in stages and not dryrun:
            if idle_minutes_to_autostop is not None:
                assert isinstance(backend, backends.CloudVmRayBackend)
                assert isinstance(handle, backends.CloudVmRayResourceHandle)
                backend.set_autostop(handle, idle_minutes_to_autostop, wait_for,
                                     down)

        if Stage.EXEC in stages:
            try:
                global_user_state.update_last_use(handle.get_cluster_name())
                job_id = backend.execute(handle,
                                         task,
                                         detach_run,
                                         dryrun=dryrun)
            finally:
                # Enables post_execute() to be run after KeyboardInterrupt.
                backend.post_execute(handle, down)

        if Stage.DOWN in stages and not dryrun:
            if down and idle_minutes_to_autostop is None:
                backend.teardown_ephemeral_storage(task)
                backend.teardown(handle, terminate=True)
    finally:
        print()
        print('\x1b[?25h', end='')  # Show cursor.
    return job_id, handle


@timeline.event
@usage_lib.entrypoint
# A launch routine will share tempfiles between steps, so we init a tempdir
# for the launch routine and gc the entire dir after launch.
@tempstore.with_tempdir
def launch(
    task: Union['sky.Task', 'sky.Dag'],
    cluster_name: Optional[str] = None,
    retry_until_up: bool = False,
    idle_minutes_to_autostop: Optional[int] = None,
    dryrun: bool = False,
    down: bool = False,
    stream_logs: bool = True,
    backend: Optional[backends.Backend] = None,
    optimize_target: common.OptimizeTarget = common.OptimizeTarget.COST,
    no_setup: bool = False,
    clone_disk_from: Optional[str] = None,
    fast: bool = False,
    # Internal only:
    # pylint: disable=invalid-name
    _quiet_optimizer: bool = False,
    _is_launched_by_jobs_controller: bool = False,
    _is_launched_by_sky_serve_controller: bool = False,
    _disable_controller_check: bool = False,
) -> Tuple[Optional[int], Optional[backends.ResourceHandle]]:
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Launches a cluster or task.

    The task's setup and run commands are executed under the task's workdir
    (when specified, it is synced to remote cluster).  The task undergoes job
    queue scheduling on the cluster.

    Currently, the first argument must be a sky.Task, or (EXPERIMENTAL advanced
    usage) a sky.Dag. In the latter case, currently it must contain a single
    task; support for pipelines/general DAGs are in experimental branches.

    Example:
        .. code-block:: python

            import sky
            task = sky.Task(run='echo hello SkyPilot')
            task.set_resources(
                sky.Resources(infra='aws', accelerators='V100:4'))
            sky.launch(task, cluster_name='my-cluster')


    Args:
        task: sky.Task, or sky.Dag (experimental; 1-task only) to launch.
        cluster_name: name of the cluster to create/reuse.  If None,
            auto-generate a name.
        retry_until_up: whether to retry launching the cluster until it is
            up.
        idle_minutes_to_autostop: automatically stop the cluster after this
            many minute of idleness, i.e., no running or pending jobs in the
            cluster's job queue. Idleness gets reset whenever setting-up/
            running/pending jobs are found in the job queue. Setting this
            flag is equivalent to running
            ``sky.launch(...)`` and then
            ``sky.autostop(idle_minutes=<minutes>)``. If set, the autostop
            config specified in the task' resources will be overridden by
            this parameter.
        down: Tear down the cluster after all jobs finish (successfully or
            abnormally). If --idle-minutes-to-autostop is also set, the
            cluster will be torn down after the specified idle time.
            Note that if errors occur during provisioning/data syncing/setting
            up, the cluster will not be torn down for debugging purposes. If
            set, the autostop config specified in the task' resources will be
            overridden by this parameter.
        dryrun: if True, do not actually launch the cluster.
        stream_logs: if True, show the logs in the terminal.
        backend: backend to use.  If None, use the default backend
            (CloudVMRayBackend).
        optimize_target: target to optimize for. Choices: OptimizeTarget.COST,
            OptimizeTarget.TIME.
        no_setup: if True, do not re-run setup commands.
        clone_disk_from: [Experimental] if set, clone the disk from the
            specified cluster. This is useful to migrate the cluster to a
            different availability zone or region.
        fast: [Experimental] If the cluster is already up and available,
            skip provisioning and setup steps.

    Raises:
        exceptions.ClusterOwnerIdentityMismatchError: if the cluster is
            owned by another user.
        exceptions.InvalidClusterNameError: if the cluster name is invalid.
        exceptions.ResourcesMismatchError: if the requested resources
            do not match the existing cluster.
        exceptions.NotSupportedError: if required features are not supported
            by the backend/cloud/cluster.
        exceptions.ResourcesUnavailableError: if the requested resources
            cannot be satisfied. The failover_history of the exception
            will be set as:
                1. Empty: iff the first-ever sky.optimize() fails to
                find a feasible resource; no pre-check or actual launch is
                attempted.
                2. Non-empty: iff at least 1 exception from either
                our pre-checks (e.g., cluster name invalid) or a region/zone
                throwing resource unavailability.
        exceptions.CommandError: any ssh command error.
        exceptions.NoCloudAccessError: if all clouds are disabled.
    Other exceptions may be raised depending on the backend.

    Returns:
      job_id: Optional[int]; the job ID of the submitted job. None if the
        backend is not CloudVmRayBackend, or no job is submitted to
        the cluster.
      handle: Optional[backends.ResourceHandle]; the handle to the cluster. None
        if dryrun.
    """

    entrypoint = task
    entrypoint.validate()
    if not _disable_controller_check:
        controller_utils.check_cluster_name_not_controller(
            cluster_name, operation_str='sky.launch')

    handle = None
    stages = None
    skip_unnecessary_provisioning = False
    # Check if cluster exists and we are doing fast provisioning
    if fast and cluster_name is not None:
        cluster_status, maybe_handle = (
            backend_utils.refresh_cluster_status_handle(cluster_name))
        if cluster_status == status_lib.ClusterStatus.INIT:
            # If the cluster is INIT, it may be provisioning. We want to prevent
            # concurrent calls from queueing up many sequential reprovision
            # attempts. Since provisioning will hold the cluster status lock, we
            # wait to hold that lock by force refreshing the status. This will
            # block until the cluster finishes provisioning, then correctly see
            # that it is UP.
            # TODO(cooperc): If multiple processes launched in parallel see that
            # the cluster is STOPPED or does not exist, they will still all try
            # to provision it, since we do not hold the lock continuously from
            # the status check until the provision call. Fixing this requires a
            # bigger refactor.
            cluster_status, maybe_handle = (
                backend_utils.refresh_cluster_status_handle(
                    cluster_name,
                    force_refresh_statuses=[
                        # If the cluster is INIT, we want to try to grab the
                        # status lock, which should block until provisioning is
                        # finished.
                        status_lib.ClusterStatus.INIT,
                    ],
                    # Wait indefinitely to obtain the lock, so that we don't
                    # have multiple processes launching the same cluster at
                    # once.
                    cluster_status_lock_timeout=-1,
                ))
        if cluster_status == status_lib.ClusterStatus.UP:
            handle = maybe_handle
            stages = [
                # Provisioning will be short-circuited if the existing
                # cluster config hash matches the calculated one.
                Stage.PROVISION,
                Stage.SYNC_WORKDIR,
                Stage.SYNC_FILE_MOUNTS,
                # Setup will be skipped if provisioning was skipped.
                Stage.SETUP,
                Stage.PRE_EXEC,
                Stage.EXEC,
                Stage.DOWN,
            ]
            skip_unnecessary_provisioning = True

    # Attach to setup if the cluster is a controller, so that user can
    # see the setup logs when inspecting the launch process to know
    # excatly what the job is waiting for.
    detach_setup = controller_utils.Controllers.from_name(cluster_name) is None

    return _execute(
        entrypoint=entrypoint,
        dryrun=dryrun,
        down=down,
        stream_logs=stream_logs,
        handle=handle,
        backend=backend,
        retry_until_up=retry_until_up,
        optimize_target=optimize_target,
        stages=stages,
        cluster_name=cluster_name,
        detach_setup=detach_setup,
        detach_run=True,
        idle_minutes_to_autostop=idle_minutes_to_autostop,
        no_setup=no_setup,
        clone_disk_from=clone_disk_from,
        skip_unnecessary_provisioning=skip_unnecessary_provisioning,
        _quiet_optimizer=_quiet_optimizer,
        _is_launched_by_jobs_controller=_is_launched_by_jobs_controller,
        _is_launched_by_sky_serve_controller=
        _is_launched_by_sky_serve_controller,
    )


@usage_lib.entrypoint
def exec(  # pylint: disable=redefined-builtin
    task: Union['sky.Task', 'sky.Dag'],
    cluster_name: str,
    dryrun: bool = False,
    down: bool = False,
    stream_logs: bool = True,
    backend: Optional[backends.Backend] = None,
) -> Tuple[Optional[int], Optional[backends.ResourceHandle]]:
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Executes a task on an existing cluster.

    This function performs two actions:

    (1) workdir syncing, if the task has a workdir specified;
    (2) executing the task's ``run`` commands.

    All other steps (provisioning, setup commands, file mounts syncing) are
    skipped.  If any of those specifications changed in the task, this function
    will not reflect those changes.  To ensure a cluster's setup is up to date,
    use ``sky.launch()`` instead.

    Execution and scheduling behavior:

    - The task will undergo job queue scheduling, respecting any specified
      resource requirement. It can be executed on any node of the cluster with
      enough resources.
    - The task is run under the workdir (if specified).
    - The task is run non-interactively (without a pseudo-terminal or
      pty), so interactive commands such as ``htop`` do not work.
      Use ``ssh my_cluster`` instead.

    Args:
        task: sky.Task, or sky.Dag (experimental; 1-task only) containing the
          task to execute.
        cluster_name: name of an existing cluster to execute the task.
        down: Tear down the cluster after all jobs finish (successfully or
            abnormally). If --idle-minutes-to-autostop is also set, the
            cluster will be torn down after the specified idle time.
            Note that if errors occur during provisioning/data syncing/setting
            up, the cluster will not be torn down for debugging purposes.
        dryrun: if True, do not actually execute the task.
        stream_logs: if True, show the logs in the terminal.
        backend: backend to use.  If None, use the default backend
            (CloudVMRayBackend).

    Raises:
        ValueError: if the specified cluster is not in UP status.
        sky.exceptions.ClusterDoesNotExist: if the specified cluster does not
            exist.
        sky.exceptions.NotSupportedError: if the specified cluster is a
            controller that does not support this operation.

    Returns:
      job_id: Optional[int]; the job ID of the submitted job. None if the
        backend is not CloudVmRayBackend, or no job is submitted to
        the cluster.
      handle: Optional[backends.ResourceHandle]; the handle to the cluster. None
        if dryrun.
    """
    entrypoint = task
    entrypoint.validate(skip_file_mounts=True)
    controller_utils.check_cluster_name_not_controller(cluster_name,
                                                       operation_str='sky.exec')

    handle = backend_utils.check_cluster_available(
        cluster_name,
        operation='executing tasks',
        check_cloud_vm_ray_backend=False,
        dryrun=dryrun)
    return _execute(
        entrypoint=entrypoint,
        dryrun=dryrun,
        down=down,
        stream_logs=stream_logs,
        handle=handle,
        backend=backend,
        stages=[
            Stage.SYNC_WORKDIR,
            Stage.EXEC,
        ],
        cluster_name=cluster_name,
        detach_run=True,
    )
