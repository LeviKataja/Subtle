"""The report: what it counts, and above all what it leaves out.

A data folder is filled with things that must never reach the report (keys, addresses, folders,
show names, the note for the translator, glossary entries, subtitle text), and the report is
searched for every one of them.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from subtle import cli, config, report
from subtle.library import Records
from subtle.model import Cue, Document
from subtle.server import Job

API_KEY = "sk-ant-api03-THIS-MUST-NOT-APPEAR-0123456789"
SECRETS = [
    API_KEY, "arr-key-0f9e8d7c6b5a", "bazarr-key-11223344", "local-key-99887766", "hook-secret-abcdef123456",
    "192.168.7.20", "nas.lan", "8989", "media-box",
    "Harbour", "harbour", "Town", "Season", "Midnight", "Ferry",
    "Inspector Hale is a woman", "Night Watch", "Yövartio",
]
LINES = ["They sent me a form to fill in.", "Minulle lähetettiin lomake täytettäväksi", "Vanha sanamuoto"]

SRT = """1
00:00:01,000 --> 00:00:11,000
Tämä rivi on aivan liian pitkä yhdelle riville, eikö olekin

2
00:00:12,000 --> 00:00:12,900
Tämä vilahtaa ohi liian nopeasti.

3
00:00:15,000 --> 00:00:19,000
Lyhyt rivi.
"""


@contextlib.contextmanager
def home_at(path):
    before = os.environ.get("SUBTLE_HOME")
    os.environ["SUBTLE_HOME"] = str(path)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("SUBTLE_HOME", None)
        else:
            os.environ["SUBTLE_HOME"] = before


def filled() -> Path:
    """A data folder as a busy install leaves it, and a library folder with one saved file."""
    home = Path(tempfile.mkdtemp(prefix="subtle-report-"))
    library = Path(tempfile.mkdtemp(prefix="media-box-")) / "Harbour Town" / "Season 1"
    library.mkdir(parents=True)
    out = library / "Harbour.Town.S01E01.fi.srt"
    out.write_text(SRT, "utf-8")
    gone = library / "Midnight Ferry (2019).fi.srt"
    gone.write_text(SRT, "utf-8")

    with home_at(home):
        config.save({**config.DEFAULTS, "target_code": "fi", "target_name": "Finnish", "api_key": API_KEY,
                     "notes": "Inspector Hale is a woman.", "glossary": [["Night Watch", "Yövartio"]],
                     "sonarr_url": "http://192.168.7.20:8989", "sonarr_key": "arr-key-0f9e8d7c6b5a",
                     "bazarr_url": "http://nas.lan:6767", "bazarr_key": "bazarr-key-11223344", "bazarr_wait": 30,
                     "local_url": "http://192.168.7.20:11434", "local_model": "gemma3:12b", "local_key": "local-key-99887766",
                     "webhook_secret": "hook-secret-abcdef123456", "path_maps": [["/tv", str(library.parent.parent)]],
                     "reach_url": "http://nas.lan:8756", "auto_series": [3, 9], "settings_version": 1})
    records = Records(home / "records.db")
    records.put(out, model="claude-haiku-5-5", reader="claude-sonnet-5-5", cost=0.021, lines=700, flagged=7, changed=14)
    records.put(gone, model="claude-haiku-5-5", cost=0.004, lines=400, flagged=0)
    gone.unlink()

    doc = Document("srt", [
        Cue(1000, 3000, LINES[0], translation=LINES[1], flags=["fast"], prev=LINES[2]),
        Cue(4000, 6000, "Fine.", translation="Hyvä."),
        Cue(7000, 9000, "Stay here.", translation="Stay here.", flags=["untranslated"], edited=True),
    ])
    job = Job("Harbour.Town.S01E01.en.srt", doc, str(library / "Harbour.Town.S01E01.en.srt"))
    job.status, job.est, job.spent = "done", {"cost": 0.02, "proof": 0.006}, {"cost": 0.021, "inp": 30000, "out": 9000, "requests": 8}
    job.last = {"units": 3, "cached": 0, "repaired": 2, "failed": 0, "inp": 28000, "out": 8500}
    job.used = {"model": "claude-haiku-5-5", "target_code": "fi"}
    job.proofed = {"model": "claude-sonnet-5-5", "lines": 3, "changed": 1, "dropped": 1, "cost": 0.006}
    job.via, job.saved_to, job.auto = "Sonarr · track inside Harbour.Town.S01E01.mkv (eng SubRip)", str(out), True
    broken = Job("Midnight Ferry (2019).en.srt", Document("srt", [Cue(0, 1000, "Hello.")]))
    broken.status = "error"
    broken.error = f"Could not write {library / 'Midnight Ferry (2019).fi.srt'}: http://nas.lan:6767 answered 500 (key {API_KEY})"
    (home / "jobs").mkdir()
    for j in (job, broken):
        (home / "jobs" / f"{j.id}.json").write_text(json.dumps(j.to_json(), ensure_ascii=False), "utf-8")

    (home / "activity.json").write_text(json.dumps([
        {"t": 1794000000, "origin": "Sonarr", "file": "Harbour.Town.S01E01.mkv", "status": "translated", "note": "Saved Harbour.Town.S01E01.fi.srt", "cost": 0.021},
        {"t": 1794000100, "origin": "Sonarr", "file": "Harbour.Town.S01E02.mkv", "status": "skipped",
         "note": f"The folder {library} cannot be opened from this machine", "cost": 0},
        {"t": 1794000200, "origin": "Sonarr", "file": "Harbour.Town.S01E03.mkv", "status": "skipped",
         "note": f"The folder {library} cannot be opened from this machine", "cost": 0},
        {"t": 1794000300, "origin": "Radarr", "file": "Midnight Ferry (2019).mkv", "status": "error",
         "note": "Bazarr at 192.168.7.20:6767 did not answer", "cost": 0},
    ]), "utf-8")
    return home


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = filled()
        with home_at(cls.home):
            cls.plain = report.build()
            cls.quoting = report.build(lines=2)

    def test_nothing_private_gets_into_it(self):
        for text in (self.plain, self.quoting):
            for secret in SECRETS:
                self.assertNotIn(secret, text)
            self.assertNotIn(str(Path(tempfile.gettempdir())), text)

    def test_subtitle_text_only_when_asked_for(self):
        for line in LINES:
            self.assertNotIn(line, self.plain)
            self.assertIn(line, self.quoting)
        self.assertIn("No subtitle text is included.", self.plain)
        self.assertIn("quotes subtitle lines", self.quoting)
        self.assertIn("[too fast, ", self.quoting)
        self.assertIn("[changed by proofreading]", self.quoting)

    def test_the_settings_that_matter_are_there_without_their_values(self):
        text = self.plain
        self.assertIn("Translating into Finnish (fi) with Haiku 5.5 · API key: saved", text)
        self.assertIn("note for the translator: 26 characters · glossary: 1 term", text)
        self.assertIn("Local model: gemma3:12b, context 4096, key set: yes", text)
        self.assertIn("Sonarr: connected · Radarr: no · Bazarr: set, waits 30 minutes · path mappings: 1", text)
        self.assertIn("Translated without asking: 2 shows", text)

    def test_saved_files_are_counted_and_measured(self):
        text = self.plain
        self.assertIn("2 files.", text)
        saved = text[text.index("== Files Subtle saved"):text.index("== The queue")]
        rows = {line.split()[1]: line.split() for line in saved.splitlines() if line.strip().startswith("file ")}
        # file, n, date, Haiku, 5.5, Sonnet, 5.5, lines, cost, flagged, changed, as, written, fast now, long now
        self.assertEqual(rows["1"][7:11], ["700", "$0.0210", "7", "14"])
        self.assertEqual(rows["1"][11:], ["as", "written", "1", "1"])  # one line too fast and one too long, as SRT has them
        self.assertEqual(rows["2"][-3:], ["gone", "-", "-"])
        self.assertIn("$0.0300", saved)  # 2.1 cents for 700 lines is 3 cents per thousand

    def test_a_file_keeps_one_label_everywhere(self):
        queue = self.plain[self.plain.index("== The queue"):self.plain.index("== The automation log")]
        self.assertIn("file 1 · srt · 3 lines · done · Haiku 5.5, read by Sonnet 5.5 · started by a webhook", queue)
        self.assertIn("source: Sonarr · track inside file 1 (eng SubRip)", queue)

    def test_the_queue_says_what_was_estimated_spent_and_flagged(self):
        text = self.plain
        self.assertIn("cost: estimated $0.0200 (of which proofreading $0.0060) · spent $0.0210 · 8 requests · 30,000 tokens in, 9,000 out", text)
        self.assertIn("last run: 3 lines, 0 from cache, 2 repaired, 0 failed, 28,000 tokens in, 8,500 out", text)
        self.assertIn("proofreading: read 3 lines, changed 1, discarded 1, $0.0060", text)
        self.assertIn("flags: too fast 1, same as source 1 · 1 line edited by hand", text)
        self.assertIn("(105% of the estimate)", text)

    def test_messages_keep_their_meaning_and_lose_their_names(self):
        text = self.plain
        self.assertIn("message: Could not write file 2: <address> answered 500 (key <key>)", text)
        self.assertIn("The folder <folder> cannot be opened from this machine", text)
        self.assertIn("Bazarr at <address> did not answer", text)
        log = text[text.index("== The automation log"):]
        self.assertRegex(log, r"skipped\s+2\s+The folder <folder>")  # the same reason twice adds up

    def test_an_empty_folder_still_gives_a_report(self):
        with home_at(tempfile.mkdtemp(prefix="subtle-report-empty-")):
            text = report.build(lines=3)
        self.assertIn("No record of saved files here", text)
        self.assertIn("The queue is empty.", text)
        self.assertIn("Nothing has been logged.", text)

    def test_a_part_that_breaks_does_not_cost_the_rest(self):
        home = Path(tempfile.mkdtemp(prefix="subtle-report-broken-"))
        (home / "records.db").write_text("this is not a database", "utf-8")
        with home_at(home):
            text = report.build()
        self.assertIn("This part could not be put together: DatabaseError", text)
        self.assertIn("== The queue ==", text)

    def test_the_command_writes_a_file_or_prints(self):
        target = Path(tempfile.mkdtemp()) / "out.txt"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["report", "--data", str(self.home), "-o", str(target)]), 0)
        self.assertIn("== Files Subtle saved ==", target.read_text("utf-8"))
        self.assertIn("Read it through before passing it on", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["report", "--data", str(self.home), "-o", "-", "--lines", "1"]), 0)
        self.assertIn(LINES[1], out.getvalue())

    def test_the_record_of_saved_files_is_only_read(self):
        before = (self.home / "records.db").read_bytes()
        with home_at(self.home):
            report.build()
        self.assertEqual(before, (self.home / "records.db").read_bytes())


class Scrubbing(unittest.TestCase):
    """Free text: error messages and log notes, which can name anything."""

    def test_paths_addresses_and_keys_become_placeholders(self):
        clean = report.Names().clean
        for text, want in [
            (r"Could not open D:\TV\Some Show\Season 1\Some Show - S01E02.mkv: permission denied", "Could not open <file>: permission denied"),
            (r"\\nas\media\tv\Some Show cannot be opened", "<folder> Show cannot be opened"),
            (r"The folder D:\Media\Some Show\Season 1 is missing", "The folder <folder> 1 is missing"),
            ("Cannot read /mnt/media/tv/Some Show/Season 1/Some Show - S01E02.en.srt", "Cannot read <file>"),
            ("Path /tv is not mapped", "Path <folder> is not mapped"),
            ("http://192.168.1.20:8989/api/v3/series?apikey=abc123 answered 401", "<address> answered 401"),
            ("Bazarr at nas.local:6767 did not answer in 30 s", "Bazarr at <address> did not answer in 30 s"),
            ("10.0.0.5 refused the connection", "<address> refused the connection"),
            ("Invalid key sk-ant-api03-AbCdEfGhIjKlMnOp", "Invalid key <key>"),
            ("Skipped Show.S01E02.1080p.WEB.mkv: no English subtitles", "Skipped <file>: no English subtitles"),
            ("Stopped at 12:30 after 3/5 requests, $0.05/file", "Stopped at 12:30 after 3/5 requests, $0.05/file"),
            ("The reply had 118 lines for 120; </i> was left open", "The reply had 118 lines for 120; </i> was left open"),
        ]:
            self.assertEqual(clean(text), want)

    def test_a_known_file_is_named_by_its_label_whatever_its_ending(self):
        names = report.Names()
        self.assertEqual(names.label(r"D:\TV\Some Show\Season 1\Some Show - S01E02.mkv"), "file 1")
        self.assertEqual(names.label("Some Show - S01E02.en.sdh.srt"), "file 1")   # its subtitles
        self.assertEqual(names.label("Some Show - S01E03.mkv"), "file 2")
        self.assertEqual(names.clean("Saved Some Show - S01E02.fi.srt next to Some Show - S01E02.mkv"), "Saved file 1 next to file 1")
        self.assertEqual(names.clean(r"D:\TV\Some Show\Season 1 has no room left"), "<folder> has no room left")
        self.assertEqual(names.clean("Some Show - S01E021.mkv is another episode"), "Some Show - <file> is another episode")


if __name__ == "__main__":
    unittest.main()
