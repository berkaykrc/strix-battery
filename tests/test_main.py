import threading
import unittest
from types import ModuleType
from unittest.mock import patch

from main import MAX_SLEEP_MINUTES, StrixTrayPystray, validate_sleep_minutes


class SleepDurationTests(unittest.TestCase):
    def test_accepts_values_that_fit_the_hid_field(self):
        self.assertEqual(validate_sleep_minutes(0), 0)
        self.assertEqual(validate_sleep_minutes(MAX_SLEEP_MINUTES), MAX_SLEEP_MINUTES)

    def test_rejects_negative_or_overflowing_values(self):
        with self.assertRaises(ValueError):
            validate_sleep_minutes(-1)
        with self.assertRaises(ValueError):
            validate_sleep_minutes(MAX_SLEEP_MINUTES + 1)


class FakePystrayIcon:
    def __init__(self, device=None):
        self.device = device
        self.visible = False
        self.detached_runs = 0
        self.blocking_runs = 0
        self.stop_count = 0
        self.icon_write_threads = []
        self.title_write_threads = []
        self._icon = None
        self._title = None

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self.icon_write_threads.append(threading.get_ident())
        self._icon = value

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self.title_write_threads.append(threading.get_ident())
        self._title = value

    def run_detached(self):
        self.detached_runs += 1

    def run(self):
        self.blocking_runs += 1
        if self.device is not None:
            self.device.query_finished.wait(timeout=1)

    def stop(self):
        self.stop_count += 1


class FakeMainLoop:
    def __init__(self):
        self.run_count = 0
        self.quit_count = 0
        self.run_error = None
        self.dispatch_callback = None

    def run(self):
        self.run_count += 1
        if self.dispatch_callback is not None:
            self.dispatch_callback()
        if self.run_error is not None:
            raise self.run_error

    def quit(self):
        self.quit_count += 1


class FakeGLib:
    def __init__(self, dispatch_timer=False):
        self.main_loop = FakeMainLoop()
        self.dispatch_timer = dispatch_timer
        self.timer_callback = None
        self.timer_interval = None
        self.removed_source_ids = []
        self.source_remove_error = None

    def MainLoop(self):
        return self.main_loop

    def timeout_add_seconds(self, interval, callback):
        self.timer_interval = interval
        self.timer_callback = callback
        if self.dispatch_timer:
            self.main_loop.dispatch_callback = callback
        return 1

    def source_remove(self, source_id):
        self.removed_source_ids.append(source_id)
        if self.source_remove_error is not None:
            raise self.source_remove_error


class FakeDevice:
    def __init__(self):
        self.query_threads = []
        self.query_finished = threading.Event()
        self.query_error = None
        self.after_query = None
        self.status = {
            "percentage": 50,
            "voltage": 4000,
            "charging": True,
            "sleep_min": 0,
        }

    def query(self):
        self.query_threads.append(threading.get_ident())
        self.query_finished.set()
        if self.query_error is not None:
            raise self.query_error
        if self.after_query is not None:
            self.after_query()
        return self.status


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeMenuItem:
    def __init__(self, text, action, **kwargs):
        self.text = text
        self.action = action
        self.submenu = action if isinstance(action, FakeMenu) else None


def find_menu_item(items, text):
    for item in items:
        if not hasattr(item, "text"):
            continue
        if item.text == text:
            return item
        if item.submenu is not None:
            match = find_menu_item(item.submenu.items, text)
            if match is not None:
                return match
    return None


class PystrayPollingTests(unittest.TestCase):
    def test_start_schedules_updates_on_the_main_loop(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        glib = FakeGLib(dispatch_timer=True)
        updates = []
        tray.update_tray = lambda: updates.append("updated")
        tray._main_loop = None
        tray._poll_source_id = None

        with patch("main._load_glib", return_value=glib):
            tray.start()

        self.assertIsNotNone(glib.timer_callback)
        self.assertEqual(glib.timer_interval, 15)
        self.assertEqual(glib.main_loop.run_count, 1)
        self.assertEqual(tray.icon.detached_runs, 1)
        self.assertEqual(tray.icon.blocking_runs, 0)
        self.assertEqual(updates, ["updated"])

    def test_polling_mutates_icon_on_main_loop_thread(self):
        device = FakeDevice()
        icon = FakePystrayIcon(device)
        icon.visible = True
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = icon
        glib = FakeGLib(dispatch_timer=True)
        main_thread = threading.get_ident()

        with patch("main._load_glib", return_value=glib), patch(
            "main.render_tray_icon", return_value=object()
        ):
            tray.start()

        self.assertEqual(device.query_threads, [main_thread])
        self.assertEqual(icon.icon_write_threads, [main_thread])
        self.assertEqual(icon.title_write_threads, [main_thread])

    def test_poll_clears_source_id_when_refresh_fails(self):
        device = FakeDevice()
        device.query_error = RuntimeError("query failed")
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        tray._poll_source_id = 1
        tray._shutting_down = False

        with self.assertRaisesRegex(RuntimeError, "query failed"):
            tray.poll()

        self.assertIsNone(tray._poll_source_id)

    def test_start_skips_automatic_polling_without_glib(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray._main_loop = None
        tray._poll_source_id = None

        with patch("main._load_glib", side_effect=ImportError), patch("builtins.print"):
            tray.start()

        self.assertEqual(tray.icon.blocking_runs, 1)
        self.assertEqual(tray.icon.detached_runs, 0)

    def test_start_cleans_up_when_main_loop_fails(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        glib.main_loop.run_error = RuntimeError("main loop failed")
        tray._main_loop = None
        tray._poll_source_id = None

        with patch("main._load_glib", return_value=glib):
            with self.assertRaisesRegex(RuntimeError, "main loop failed"):
                tray.start()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_stops_polling_icon_and_main_loop(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        tray.quit()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_continues_cleanup_when_timer_removal_fails(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        glib.source_remove_error = RuntimeError("timer cleanup failed")
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        with self.assertRaisesRegex(RuntimeError, "timer cleanup failed"):
            tray.quit()

        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_is_idempotent(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        tray.quit()
        tray.quit()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_poll_stops_when_icon_is_hidden(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray._poll_source_id = 1
        tray._shutting_down = False
        updates = []
        tray.update_tray = lambda: updates.append("updated")

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(updates, [])

    def test_poll_does_not_update_after_shutdown_starts(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        tray._poll_source_id = 1
        tray._shutting_down = True
        updates = []
        tray.update_tray = lambda: updates.append("updated")

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(updates, [])

    def test_poll_does_not_write_icon_after_shutdown_during_query(self):
        device = FakeDevice()
        icon = FakePystrayIcon()
        icon.visible = True
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = icon
        tray._glib = None
        tray._main_loop = None
        tray._poll_source_id = 1
        tray._shutting_down = False
        device.after_query = tray.quit

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(icon.icon_write_threads, [])
        self.assertEqual(icon.title_write_threads, [])

    def test_quit_menu_item_uses_full_shutdown_path(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        updates = []
        tray.update_tray = lambda: updates.append("updated")
        quit_calls = []
        tray.quit = lambda: quit_calls.append("quit")
        pystray = ModuleType("pystray")
        pystray.Menu = FakeMenu
        pystray.MenuItem = FakeMenuItem

        with patch.dict("sys.modules", {"pystray": pystray}):
            menu = tray.create_menu()

        refresh_item = find_menu_item(menu.items, "Refresh Now")
        refresh_item.action()
        self.assertEqual(updates, ["updated"])

        quit_item = find_menu_item(menu.items, "Quit")
        quit_item.action()
        self.assertEqual(quit_calls, ["quit"])
        self.assertEqual(tray.icon.stop_count, 0)


if __name__ == "__main__":
    unittest.main()