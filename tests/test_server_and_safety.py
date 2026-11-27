"""Unit & integration tests for the Podman server, safety state machine, and web profile."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zoom_faust_fx import find_stomphacks_dir
from zoom_faust_fx.postprocess import (
    FaustAllowlistError,
    validate_expanded_faust,
    validate_faust_allowlist,
)
from zoom_faust_fx.server.build_manager import BuildManager
from zoom_faust_fx.server.midi_relay import (
    ChecklistAck,
    PedalBusyError,
    PedalSessionManager,
    PedalTransferError,
    RescueLockError,
    SafetyGateError,
    WebMidiTransport,
)


def _has_stomphacks() -> bool:
    try:
        find_stomphacks_dir()
        return True
    except FileNotFoundError:
        return False


def _load_mock_pedal(sh_dir: Path):
    for sub in ("zoom-zt2", "tools", "tools-pedal"):
        p = str(sh_dir / sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    import mock_pedal
    import zoomzt2

    return mock_pedal, zoomzt2


def _make_flst(zoomzt2, entries):
    groups = {}
    for name, ver, eid in entries:
        groups.setdefault((eid >> 24) & 0xFF, []).append(
            dict(effect=name, version=ver, id=eid, installed=1)
        )
    cfg = [
        dict(name="FLST_SEQ"),
        [dict(group=g, groupname=g, effects=effs) for g, effs in sorted(groups.items())],
    ]
    return zoomzt2.ZT2.build(cfg)


class TestBuildManagerAndProfiles(unittest.TestCase):
    def test_containerfile_and_compose_exist(self) -> None:
        cf = (ROOT / "Containerfile").read_text(encoding="utf-8")
        cy = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("--platform=linux/amd64", cf)
        self.assertIn("STOMPHACKS_DIR=/opt/stomphacks", cf)
        self.assertIn("ZOOM_TI_CGT=/opt/ti/c6000", cf)
        self.assertIn("platform: linux/amd64", cy)
        self.assertIn("linux-midi", cy)

    def test_github_pages_profile_omits_ide_and_pedal_ui(self) -> None:
        index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("dspEditor", index_html)
        self.assertNotIn("webmidi.js", index_html)
        self.assertNotIn("safeConnectBtn", index_html)

        dist_dir = ROOT / "web" / "dist"
        if dist_dir.is_dir():
            self.assertFalse((dist_dir / "studio.html").exists())
            self.assertFalse((dist_dir / "studio.js").exists())
            self.assertFalse((dist_dir / "webmidi.js").exists())

    def test_build_manager_job_creation_and_artifact_traversal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bm = BuildManager(work_root=tmp)
            job = bm.create_job(
                dsp_code='declare name "TestFx";\nprocess = _, _;\n',
                name="TestFx",
                icon_bitmap=[[1, 0], [0, 1]],
            )
            self.assertEqual(job.status, "queued")
            self.assertTrue((job.work_dir / "input" / "TestFx.dsp").is_file())
            self.assertIsNone(bm.get_artifact_path(job.build_id, "../pyproject.toml"))
            self.assertIsNone(bm.get_artifact_path("missing", "foo.zd2"))


class TestWebMidiRelayTransport(unittest.TestCase):
    def test_webmidi_transport_send_recv_and_timeout(self) -> None:
        sent: list[dict] = []

        async def _run():
            loop = asyncio.get_running_loop()

            async def fake_send(payload: dict):
                sent.append(payload)

            transport = WebMidiTransport(loop, fake_send)

            def worker():
                transport.send([0x7E, 0x7F, 0x06, 0x01])
                transport.push_rx([0x7E, 0x7F, 0x06, 0x02, 0x52])
                reply = transport.recv(1.0)
                timeout_reply = transport.recv(0.05)
                return reply, timeout_reply

            reply, timeout_reply = await asyncio.to_thread(worker)
            self.assertEqual(sent, [{"type": "sysex_tx", "data": [0x7E, 0x7F, 0x06, 0x01]}])
            self.assertEqual(reply, [0x7E, 0x7F, 0x06, 0x02, 0x52])
            self.assertIsNone(timeout_reply)

        asyncio.run(_run())


class TestPedalSafetyAndMockPedal(unittest.TestCase):
    def setUp(self) -> None:
        if not _has_stomphacks():
            self.skipTest("stomphacks checkout not found")
        self.sh_dir = find_stomphacks_dir()
        self.mock_pedal, self.zoomzt2 = _load_mock_pedal(self.sh_dir)
        self.zd2_fixture = ROOT / "effects" / "anlgdrv" / "gen" / "build" / "XXANLGDR.ZD2"
        if not self.zd2_fixture.is_file():
            self.skipTest("XXANLGDR.ZD2 fixture not built")

        self._tmp = tempfile.TemporaryDirectory()
        tmp_p = Path(self._tmp.name)
        self.pm = PedalSessionManager(
            stomphacks_dir=self.sh_dir,
            session_dir=tmp_p / "session",
            backup_dir=tmp_p / "backup",
        )
        # Seed a valid FLST_SEQ.ZT2 containing group 7 (SFX) so XXANLGDR (group 7 or 2) fits cleanly
        flst_init = _make_flst(
            self.zoomzt2,
            [
                ("DELAY.ZD2", "1.00", 0x08000010),
                ("CHORUS.ZD2", "1.00", 0x06000020),
                ("STOCKSFX.ZD2", "1.00", 0x07000001),
            ],
        )
        self.pedal = self.mock_pedal.MockPedal(
            files={"FLST_SEQ.ZT2": flst_init},
            autosave=1,
            disk_available=4 * 1024 * 1024,
        )

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def _valid_checklist(self) -> ChecklistAck:
        return ChecklistAck(
            autosave_off_confirmed=True,
            backup_confirmed=True,
            unsaved_patch_only_confirmed=True,
            no_power_cycle_confirmed=True,
        )

    def test_autosave_stuck_on_refuses_connect(self) -> None:
        self.pedal.autosave_stuck_on = True
        with self.assertRaises(SafetyGateError):
            self.pm.connect(transport=self.pedal)
        self.assertFalse(self.pm.autosave_off_confirmed)

    def test_backup_gate_and_checklist_and_margin_gates(self) -> None:
        res = self.pm.connect(transport=self.pedal)
        self.assertTrue(res["connected"])
        self.assertTrue(res["autosave_off_confirmed"])
        self.assertEqual(self.pedal.autosave, 0)

        tok = self.pm.issue_confirmation_token("install", "XXANLGDR.ZD2")
        # 1. Backup gate blocks install before backup
        with self.assertRaises(SafetyGateError):
            self.pm.install(
                zd2_path=self.zd2_fixture,
                report_path=self.zd2_fixture.parent / "REPORT.md",
                checklist=self._valid_checklist(),
                confirmation_token=tok,
                transport=self.pedal,
            )

        # Complete backup
        b_res = self.pm.backup(transport=self.pedal)
        self.assertTrue(b_res["backup_completed"])

        # 2. Incomplete checklist blocks install
        bad_chk = ChecklistAck(autosave_off_confirmed=True)
        with self.assertRaises(SafetyGateError):
            self.pm.install(
                zd2_path=self.zd2_fixture,
                report_path=self.zd2_fixture.parent / "REPORT.md",
                checklist=bad_chk,
                confirmation_token=tok,
                transport=self.pedal,
            )

        # 3. Free margin below 32 KB floor blocks install
        with self.assertRaises(SafetyGateError):
            self.pm.install(
                zd2_path=self.zd2_fixture,
                report_path=self.zd2_fixture.parent / "REPORT.md",
                checklist=self._valid_checklist(),
                confirmation_token=tok,
                free_margin_kb=16,
                transport=self.pedal,
            )

    def test_full_connect_backup_install_uninstall_on_mock_pedal(self) -> None:
        self.pm.connect(transport=self.pedal)
        self.pm.backup(transport=self.pedal)

        tok_in = self.pm.issue_confirmation_token("install", "XXANLGDR.ZD2")
        in_res = self.pm.install(
            zd2_path=self.zd2_fixture,
            report_path=self.zd2_fixture.parent / "REPORT.md",
            checklist=self._valid_checklist(),
            confirmation_token=tok_in,
            transport=self.pedal,
        )
        self.assertTrue(in_res["installed"])
        self.assertIn("XXANLGDR.ZD2", self.pedal.files)
        self.assertIn("XXANLGDR.ZIC", self.pedal.files)

        tok_un = self.pm.issue_confirmation_token("uninstall", "XXANLGDR.ZD2")
        un_res = self.pm.uninstall(
            zd2_path=self.zd2_fixture,
            checklist=self._valid_checklist(),
            confirmation_token=tok_un,
            transport=self.pedal,
        )
        self.assertTrue(un_res["uninstalled"])
        self.assertNotIn("XXANLGDR.ZD2", self.pedal.files)
        self.assertNotIn("XXANLGDR.ZIC", self.pedal.files)

    def test_single_flight_mutex_rejects_concurrent_operation(self) -> None:
        self.pm._acquire_mutex("install")
        try:
            with self.assertRaises(PedalBusyError):
                self.pm.connect(transport=self.pedal)
        finally:
            self.pm._release_mutex()

    def test_wedged_pedal_triggers_rescue_lock_and_rescue_clears_it(self) -> None:
        self.pm.connect(transport=self.pedal)
        self.pm.backup(transport=self.pedal)

        # Inject a dead fault during FLST_SEQ.ZT2 write block (60 23) so replace_flst fails unverified
        self.pedal.fault = self.mock_pedal.Fault("dead", sub=0x23, name="FLST_SEQ.ZT2")
        tok = self.pm.issue_confirmation_token("install", "XXANLGDR.ZD2")
        with self.assertRaises(PedalTransferError) as ctx:
            self.pm.install(
                zd2_path=self.zd2_fixture,
                report_path=self.zd2_fixture.parent / "REPORT.md",
                checklist=self._valid_checklist(),
                confirmation_token=tok,
                transport=self.pedal,
            )
        self.assertTrue(ctx.exception.rescue_required)
        self.assertTrue(self.pm.rescue_locked)

        # Further installs are blocked while rescue_locked
        with self.assertRaises(RescueLockError):
            self.pm.install(
                zd2_path=self.zd2_fixture,
                report_path=self.zd2_fixture.parent / "REPORT.md",
                checklist=self._valid_checklist(),
                confirmation_token=tok,
                transport=self.pedal,
            )

        # Clear dead state on mock (pedal stayed powered) and run rescue --write-back from session backup
        self.pedal._dead = False
        self.pedal.fault = None
        tok_rescue = self.pm.issue_confirmation_token("rescue", "FLST_SEQ.ZT2")
        r_res = self.pm.rescue(
            write_back=True,
            flst_backup_path=self.pm.backup_dir / "FLST_SEQ.ZT2",
            checklist=self._valid_checklist(),
            confirmation_token=tok_rescue,
            transport=self.pedal,
        )
        self.assertTrue(r_res["rescued"])
        self.assertFalse(self.pm.rescue_locked)

    def test_allocate_diy_id_avoids_stock_and_device_ids(self) -> None:
        # Put 0x07001000 on the mock pedal so allocator must skip it and return 0x07001001
        flst_with_1000 = _make_flst(
            self.zoomzt2,
            [
                ("DELAY.ZD2", "1.00", 0x08000010),
                ("STOCKSFX.ZD2", "1.00", 0x07000001),
                ("XXCUSTOM.ZD2", "1.00", 0x07001000),
            ],
        )
        self.pedal.files["FLST_SEQ.ZT2"] = flst_with_1000
        res = self.pm.allocate_diy_id(group=0x07, from_device=True, transport=self.pedal)
        self.assertEqual(res["id"], "07001001")
        self.assertEqual(res["source"], "device")
        self.assertEqual(res["resident_count"], 3)


class TestFastAPIAppOptional(unittest.TestCase):
    def test_fastapi_endpoints_when_installed(self) -> None:
        if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
            self.skipTest("fastapi/httpx not installed in this Python environment")
        from fastapi.testclient import TestClient
        from zoom_faust_fx.server.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            bm = BuildManager(work_root=tmp_p / "jobs")
            pm = PedalSessionManager(
                session_dir=tmp_p / "session",
                backup_dir=tmp_p / "backup",
            )
            app = create_app(build_manager=bm, pedal_manager=pm)
            client = TestClient(app)

            caps = client.get("/api/capabilities")
            self.assertEqual(caps.status_code, 200)
            self.assertIn("profile", caps.json())

            tok = client.post(
                "/api/pedal/confirm-token",
                json={"action": "install", "target": "XXANLGDR.ZD2"},
            )
            self.assertEqual(tok.status_code, 200)
            self.assertIn("confirmation_token", tok.json())

            al = client.get("/api/faust/allowlist")
            self.assertEqual(al.status_code, 200)
            self.assertIn("ro", al.json()["allowed_modules"])
            self.assertIn("dattorro_rev", al.json()["allowed_module_functions"]["re"])
            self.assertIn("hbargraph", al.json()["unsupported_ui_elements"])


class TestFaustAllowlist(unittest.TestCase):
    def test_all_repo_effects_pass_allowlist(self) -> None:
        dsp_files = sorted((ROOT / "effects").glob("*/*.dsp"))
        self.assertGreaterEqual(len(dsp_files), 22)
        for dsp in dsp_files:
            validate_faust_allowlist(dsp.read_text(encoding="utf-8"))

    def test_disallowed_module_import_and_ffi_fail_closed(self) -> None:
        with self.assertRaises(FaustAllowlistError):
            validate_faust_allowlist('import("stdfaust.lib");\nprocess = pm.guitar(0.5, 0.5, 0.5), pm.guitar(0.5, 0.5, 0.5);\n')
        with self.assertRaises(FaustAllowlistError):
            validate_faust_allowlist('import("reverbs.lib");\nprocess = _, _;\n')
        with self.assertRaises(FaustAllowlistError):
            validate_faust_allowlist('f = ffunction(float foo(float), <foo.h>, "");\nprocess = f;\n')
        with self.assertRaises(FaustAllowlistError) as ctx:
            validate_faust_allowlist('import("stdfaust.lib");\nprocess = de.delay(3000, 100), de.delay(3000, 100);\n')
        self.assertIn("power of two", str(ctx.exception))
        self.assertIn("4096", str(ctx.exception))

        # Per-module function allowlist: re.dattorro_rev passes, re.stereo_freeverb & co.RMS_FBFFcompressor_N_chan fail
        validate_faust_allowlist('import("stdfaust.lib");\nprocess = re.dattorro_rev(0, 0.99, 0.75, 0.625, 0.5, 0.7, 0.5, 0.001);\n')
        with self.assertRaises(FaustAllowlistError) as re_ctx:
            validate_faust_allowlist('import("stdfaust.lib");\nprocess = re.stereo_freeverb(0.5, 0.5, 0.5, 0.5);\n')
        self.assertIn("zm_freeverb", str(re_ctx.exception))
        with self.assertRaises(FaustAllowlistError):
            validate_faust_allowlist('import("stdfaust.lib");\nprocess = co.RMS_FBFFcompressor_N_chan(0.4, -20, 0.02, 0.12, 6, 0, 0.5, 0.3, _, 2);\n')

    def test_expanded_faust_catches_transitive_bargraph_and_ffi(self) -> None:
        # Standard math.h fconstant (from ma.SR) is allowed
        validate_expanded_faust("process = fconstant(int fSamplingFreq, <math.h>);")
        # Transitive hbargraph / non-math.h ffunction must fail closed
        with self.assertRaises(FaustAllowlistError):
            validate_expanded_faust('process = hbargraph("Meter", 0, 1);')
        with self.assertRaises(FaustAllowlistError):
            validate_expanded_faust('process = ffunction(float custom(float), <custom.h>, "");')
