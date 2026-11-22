"""Точка входа контейнера панели: автосборка образа воркера, затем app.py.

Если образа воркера (WORKER_IMAGE) в демоне нет, собирает его из зашитого
в образ панели контекста /worker_build (tar-поток уходит демону через
docker.sock — хостовые пути не нужны). Отключается WORKER_AUTO_BUILD=0.
Сбой автосборки не роняет панель: образ можно собрать вручную позже.
"""

import io
import os
import sys
import tarfile

WORKER_BUILD_CONTEXT = "/worker_build"


def ensure_worker_image(image, client=None, log=print):
    """Проверяет наличие образа воркера и собирает его при отсутствии.

    Возвращает True, если образ есть (или собран), False при ошибке.
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
    if os.environ.get("WORKER_AUTO_BUILD", "1") != "0":
        try:
            ensure_worker_image(image)
        except Exception as exc:
            print("[entrypoint] автосборка воркера не удалась: {}".format(exc),
                  file=sys.stderr)
            print("[entrypoint] панель стартует; соберите образ вручную: "
                  "docker build -t {} <repo>/worker".format(image), file=sys.stderr)
    os.execvp("python", ["python", "app.py"])


if __name__ == "__main__":
    main()
