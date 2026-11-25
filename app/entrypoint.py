"""Точка входа контейнера панели: обеспечение образа воркера, затем app.py.

Если образа воркера (WORKER_IMAGE) в демоне нет, сначала пытается скачать
его из реестра (WORKER_REGISTRY_IMAGE, по умолчанию GHCR проекта) и
перетегировать в локальное имя; реестр недоступен — собирает из зашитого
в образ панели контекста /worker_build (tar-поток уходит демону через
docker.sock — хостовые пути не нужны). Отключается WORKER_AUTO_BUILD=0.
Сбой не роняет панель: образ можно собрать/скачать вручную позже.
"""

import io
import os
import sys
import tarfile

WORKER_BUILD_CONTEXT = "/worker_build"
DEFAULT_REGISTRY_IMAGE = "ghcr.io/svaganov/audio-worker:2.4.2"


def ensure_worker_image(image, registry_image=None, client=None, log=print):
    """Проверяет наличие образа воркера; при отсутствии — pull из реестра,
    иначе сборка из зашитого контекста.

    Возвращает True, если образ есть (получен любым способом).
    Ошибка сборки пробрасывается (после исчерпания остальных путей);
    ошибка pull — нет, она лишь переключает на сборку.
    client подставляется в тестах (мок Docker API).
    """
    if client is None:
        import docker
        client = docker.from_env()
    import docker.errors
    try:
        client.images.get(image)
        log("[entrypoint] образ воркера {} уже есть".format(image))
        return True
    except docker.errors.ImageNotFound:
        pass

    if registry_image:
        try:
            log("[entrypoint] скачиваю образ воркера {} ...".format(registry_image))
            pulled = client.images.pull(registry_image)
            if registry_image != image:
                pulled.tag(image)
            log("[entrypoint] образ воркера {} скачан из реестра".format(image))
            return True
        except Exception as exc:
            log("[entrypoint] реестр недоступен ({}), пробую собрать локально".format(exc))

    log("[entrypoint] собираю образ воркера {} из {} ...".format(image, WORKER_BUILD_CONTEXT))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(WORKER_BUILD_CONTEXT, arcname=".")
    buf.seek(0)
    client.images.build(fileobj=buf, custom_context=True, tag=image, rm=True)
    log("[entrypoint] образ воркера {} собран".format(image))
    return True


def main():
    image = os.environ.get("WORKER_IMAGE", "audio-worker:2.4.2")
    registry_image = os.environ.get("WORKER_REGISTRY_IMAGE", DEFAULT_REGISTRY_IMAGE)
    if os.environ.get("WORKER_AUTO_BUILD", "1") != "0":
        try:
            ensure_worker_image(image, registry_image=registry_image)
        except Exception as exc:
            print("[entrypoint] получение образа воркера не удалось: {}".format(exc),
                  file=sys.stderr)
            print("[entrypoint] панель стартует; получите образ вручную: "
                  "docker pull ghcr.io/svaganov/audio-worker:<ver> или "
                  "docker build -t {} <repo>/worker".format(image), file=sys.stderr)
    os.execvp("python", ["python", "app.py"])


if __name__ == "__main__":
    main()
