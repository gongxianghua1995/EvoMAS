"""Fail-closed offline container creation for the Verified evaluation harness."""


def verify_network(attrs):
    mode = attrs.get('HostConfig', {}).get('NetworkMode')
    networks = attrs.get('NetworkSettings', {}).get('Networks', {})
    if mode != 'none' or set(networks) - {'none'}:
        raise RuntimeError('Evaluation container must use network=none')


def create_offline_container(test_spec, client, run_id, logger):
    client.images.get(test_spec.image)  # Missing image is an error, never build/pull.
    container = client.containers.create(
        image=test_spec.image, name=f'sweb.eval.{test_spec.instance_id.lower()}.{run_id}',
        user='root', detach=True, network_mode='none',
        command='tail -f /dev/null', cap_add=['SYS_ADMIN'],
    )
    try:
        container.start()
        container.reload()
        verify_network(container.attrs)
        logger.info('Evaluation container isolation verified: network=none')
    except Exception:
        container.remove(force=True)
        raise
    return container
