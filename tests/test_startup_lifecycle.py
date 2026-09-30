"""Regression for the exact nested listener starter, without network or hardware."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


class StartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracked = []
        self.background = []
        self.completed = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

        async def listen(*, callback):
            self.assertIs(callback, self.callback)
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closed.set()

        def tracked(coro):
            task = asyncio.create_task(coro)
            self.tracked.append(task)
            return task

        def background(hass, coro, name):
            self.assertIs(hass, self.hass)
            self.assertEqual(name, "loxone_websocket_listener")
            task = asyncio.create_task(coro, name=name)
            self.background.append(task)
            return task

        self.callback = object()
        self.hass = SimpleNamespace(async_create_task=tracked)
        self.entry = SimpleNamespace(async_create_background_task=background)
        self.coordinator = SimpleNamespace(api=SimpleNamespace(start_listening=listen))
        source = Path(__file__).parents[1] / "custom_components/loxone/__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry")
        start = next(n for n in setup.body if isinstance(n, ast.FunctionDef) and n.name == "start_event")
        namespace = {"hass": self.hass, "config_entry": self.entry,
                     "coordinator": self.coordinator, "message_callback": self.callback,
                     "handle_task_result": self.completed.append}
        exec(compile(ast.Module(body=[start], type_ignores=[]), str(source), "exec"), namespace)
        namespace["start_event"]()
        await asyncio.wait_for(self.started.wait(), 1)

    async def asyncTearDown(self):
        for task in self.tracked + self.background:
            task.cancel()
        await asyncio.gather(*self.tracked, *self.background, return_exceptions=True)

    async def test_infinite_listener_does_not_join_startup_tasks(self):
        # Mirrors the relevant startup barrier: tracked work must terminate.
        await asyncio.wait_for(asyncio.gather(*self.tracked), 0.05)
        self.assertEqual(len(self.background), 1)
        self.assertIs(self.coordinator._listening_task, self.background[0])
        self.assertFalse(self.background[0].done())

    async def test_owned_listener_remains_cancellable_and_callback_registered(self):
        task = self.coordinator._listening_task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(self.closed.is_set())
        self.assertIn(task, self.completed)


if __name__ == "__main__":
    unittest.main()
