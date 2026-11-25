"""Тесты entrypoint панели: получение образа воркера (мок Docker API).

Приоритет: локальный образ → pull из реестра (+retag) → сборка из контекста.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("entrypoint", APP_DIR / "entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeImage:
    def __init__(self):
        self.tags = []

    def tag(self, name):
        self.tags.append(name)
        return True


class FakeImages:
    def __init__(self, present, pull_error=None):
        self.present = present
        self.pull_error = pull_error
        self.build_calls = []
        self.pull_calls = []

    def get(self, name):
        import docker.errors
        if name in self.present:
            return FakeImage()
        raise docker.errors.ImageNotFound("no such image")

    def pull(self, name):
        self.pull_calls.append(name)
        if self.pull_error:
            raise self.pull_error
        self.last_pulled = FakeImage()
        return self.last_pulled

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        return (object(), [])


class FakeClient:
    def __init__(self, present=(), pull_error=None):
        self.images = FakeImages(set(present), pull_error=pull_error)


class EnsureWorkerImageTest(unittest.TestCase):
    def setUp(self):
        self.entrypoint = load_entrypoint()

    def test_image_present_no_pull_no_build(self):
        client = FakeClient(present=["audio-worker:2.4.2"])
        logs = []
        ok = self.entrypoint.ensure_worker_image(
            "audio-worker:2.4.2", registry_image="ghcr.io/x/y:2.4.2",
            client=client, log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(client.images.pull_calls, [])
        self.assertEqual(client.images.build_calls, [])
        self.assertTrue(any("уже есть" in line for line in logs))

    def test_missing_pulls_and_retags(self):
        client = FakeClient()
        ok = self.entrypoint.ensure_worker_image(
            "audio-worker:2.4.2", registry_image="ghcr.io/x/y:2.4.2",
            client=client, log=lambda line: None)
        self.assertTrue(ok)
        self.assertEqual(client.images.pull_calls, ["ghcr.io/x/y:2.4.2"])
        self.assertEqual(client.images.build_calls, [])
        # после pull локальное имя ставится тегом на скачанный образ
        self.assertEqual(client.images.last_pulled.tags, ["audio-worker:2.4.2"])

    def test_missing_pull_same_name_no_retag(self):
        client = FakeClient()
        ok = self.entrypoint.ensure_worker_image(
            "ghcr.io/x/y:2.4.2", registry_image="ghcr.io/x/y:2.4.2",
            client=client, log=lambda line: None)
        self.assertTrue(ok)
        self.assertEqual(client.images.pull_calls, ["ghcr.io/x/y:2.4.2"])
        # имена совпадают — ретег не нужен
        self.assertEqual(client.images.last_pulled.tags, [])

    def test_pull_failure_falls_back_to_build(self):
        client = FakeClient(pull_error=RuntimeError("registry down"))
        worker_dir = str(Path(__file__).resolve().parents[1] / "worker")
        with mock.patch.object(self.entrypoint, "WORKER_BUILD_CONTEXT", worker_dir):
            ok = self.entrypoint.ensure_worker_image(
                "audio-worker:9.9.9", registry_image="ghcr.io/x/y:9.9.9",
                client=client, log=lambda line: None)
        self.assertTrue(ok)
        self.assertEqual(client.images.pull_calls, ["ghcr.io/x/y:9.9.9"])
        self.assertEqual(len(client.images.build_calls), 1)
        call = client.images.build_calls[0]
        self.assertEqual(call["tag"], "audio-worker:9.9.9")
        self.assertTrue(call["custom_context"])
        # Контекст ушёл tar-потоком, в корне — Dockerfile воркера.
        import tarfile
        call["fileobj"].seek(0)
        with tarfile.open(fileobj=call["fileobj"]) as tar:
            self.assertIn("./Dockerfile", tar.getnames())

    def test_no_registry_image_builds_directly(self):
        client = FakeClient()
        worker_dir = str(Path(__file__).resolve().parents[1] / "worker")
        with mock.patch.object(self.entrypoint, "WORKER_BUILD_CONTEXT", worker_dir):
            ok = self.entrypoint.ensure_worker_image(
                "audio-worker:9.9.9", registry_image=None,
                client=client, log=lambda line: None)
        self.assertTrue(ok)
        self.assertEqual(client.images.pull_calls, [])
        self.assertEqual(len(client.images.build_calls), 1)

    def test_build_error_propagates_after_pull_failure(self):
        class BrokenImages(FakeImages):
            def build(self, **kwargs):
                raise RuntimeError("boom")

        client = FakeClient(pull_error=RuntimeError("registry down"))
        client.images = BrokenImages(set(), pull_error=RuntimeError("registry down"))
        worker_dir = str(Path(__file__).resolve().parents[1] / "worker")
        with mock.patch.object(self.entrypoint, "WORKER_BUILD_CONTEXT", worker_dir):
            with self.assertRaises(RuntimeError):
                self.entrypoint.ensure_worker_image(
                    "audio-worker:2.4.2", registry_image="ghcr.io/x/y:2.4.2",
                    client=client, log=lambda line: None)


class MainFlagTest(unittest.TestCase):
    def setUp(self):
        self.entrypoint = load_entrypoint()

    def _run_main(self, env):
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(self.entrypoint, "ensure_worker_image") as ensure, \
             mock.patch("os.execvp") as execvp:
            self.entrypoint.main()
        return ensure, execvp

    def test_autobuild_on_by_default(self):
        env = {k: v for k, v in os.environ.items()}
        env.pop("WORKER_AUTO_BUILD", None)
        env.pop("WORKER_IMAGE", None)
        env.pop("WORKER_REGISTRY_IMAGE", None)
        ensure, execvp = self._run_main(env)
        ensure.assert_called_once_with(
            "audio-worker:2.4.2",
            registry_image=self.entrypoint.DEFAULT_REGISTRY_IMAGE)
        execvp.assert_called_once_with("python", ["python", "app.py"])

    def test_registry_image_from_env(self):
        env = dict(os.environ, WORKER_REGISTRY_IMAGE="ghcr.io/acme/w:1.0")
        env.pop("WORKER_AUTO_BUILD", None)
        ensure, _ = self._run_main(env)
        ensure.assert_called_once_with(
            "audio-worker:2.4.2", registry_image="ghcr.io/acme/w:1.0")

    def test_autobuild_disabled_by_env(self):
        env = dict(os.environ, WORKER_AUTO_BUILD="0")
        ensure, execvp = self._run_main(env)
        ensure.assert_not_called()
        execvp.assert_called_once_with("python", ["python", "app.py"])

    def test_autobuild_failure_does_not_block_panel(self):
        env = {k: v for k, v in os.environ.items()}
        env.pop("WORKER_AUTO_BUILD", None)
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(self.entrypoint, "ensure_worker_image",
                               side_effect=RuntimeError("no docker")), \
             mock.patch("os.execvp") as execvp:
            self.entrypoint.main()
        execvp.assert_called_once_with("python", ["python", "app.py"])


if __name__ == "__main__":
    unittest.main()
