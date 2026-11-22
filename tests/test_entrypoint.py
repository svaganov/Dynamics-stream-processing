"""Тесты entrypoint панели: автосборка образа воркера (мок Docker API)."""

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


class FakeImages:
    def __init__(self, present):
        self.present = present
        self.build_calls = []

    def get(self, name):
        import docker.errors
        if name in self.present:
            return object()
        raise docker.errors.ImageNotFound("no such image")

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        return (object(), [])


class FakeClient:
    def __init__(self, present=()):
        self.images = FakeImages(set(present))


class EnsureWorkerImageTest(unittest.TestCase):
    def setUp(self):
        self.entrypoint = load_entrypoint()

    def test_image_present_no_build(self):
        client = FakeClient(present=["audio-worker:2.4.2"])
        logs = []
        ok = self.entrypoint.ensure_worker_image("audio-worker:2.4.2", client=client, log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(client.images.build_calls, [])
        self.assertTrue(any("уже есть" in line for line in logs))

    def test_image_missing_triggers_build(self):
        client = FakeClient()
        worker_dir = str(Path(__file__).resolve().parents[1] / "worker")
        with mock.patch.object(self.entrypoint, "WORKER_BUILD_CONTEXT", worker_dir):
            ok = self.entrypoint.ensure_worker_image("audio-worker:9.9.9", client=client,
                                                     log=lambda line: None)
        self.assertTrue(ok)
        self.assertEqual(len(client.images.build_calls), 1)
        call = client.images.build_calls[0]
        self.assertEqual(call["tag"], "audio-worker:9.9.9")
        self.assertTrue(call["custom_context"])
        # Контекст ушёл tar-потоком, в корне — Dockerfile воркера.
        import tarfile
        call["fileobj"].seek(0)
        with tarfile.open(fileobj=call["fileobj"]) as tar:
            self.assertIn("./Dockerfile", tar.getnames())

    def test_build_error_propagates(self):
        class BrokenImages(FakeImages):
            def build(self, **kwargs):
                raise RuntimeError("boom")

        client = FakeClient()
        client.images = BrokenImages(set())
        worker_dir = str(Path(__file__).resolve().parents[1] / "worker")
        with mock.patch.object(self.entrypoint, "WORKER_BUILD_CONTEXT", worker_dir):
            with self.assertRaises(RuntimeError):
                self.entrypoint.ensure_worker_image("audio-worker:2.4.2", client=client,
                                                    log=lambda line: None)


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
        ensure, execvp = self._run_main(env)
        ensure.assert_called_once()
        execvp.assert_called_once_with("python", ["python", "app.py"])

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
