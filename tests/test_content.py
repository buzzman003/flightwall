"""Source fidelity, bounded artwork and opt-in/billing boundaries."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, PngImagePlugin

from flightwall.config import private_json
from flightwall.content import SourceContent, _date, _url, _request


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


def image_upload(format="PNG", **extra):
    output = io.BytesIO()
    image = Image.new("RGB", (80, 40), (200, 60, 20))
    if format == "JPEG":
        exif = Image.Exif()
        exif[270] = "PRIVATE_DESCRIPTION_AND_LOCATION"
        exif[274] = 6
        image.save(output, "JPEG", exif=exif)
    else:
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "PRIVATE_DESCRIPTION_AND_LOCATION")
        image.save(output, "PNG", pnginfo=info)
    return {"owner": "lisa", "kind": "artwork", "title": "A favorite painting",
            "image_data": "data:image/" + ("jpeg" if format == "JPEG" else "png") + ";base64," + base64.b64encode(output.getvalue()).decode(), **extra}


def openai_response(text="Arrange three small drawings into a quiet botanical study."):
    return json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}).encode()


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = {"inspiration_enabled": True, "inspiration_sources": "[]", "inspiration_topics": "botanical artwork and calm interiors",
                       "health_enabled": False, "health_query": "", "health_max_age_hours": 168,
                       "openai_enabled": False, "openai_daily_limit": 2, "openai_model": "gpt-5-mini", "hotel_details_enabled": False}
        self.content = SourceContent(self.root, lambda: deepcopy(self.config))
        self.clock = patch("flightwall.content._now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_disabled_features_make_no_network_or_generation_calls(self):
        self.config.update(inspiration_enabled=False, health_enabled=False, openai_enabled=False)
        with patch("flightwall.content._request") as network:
            self.content.refresh()
            self.assertFalse(self.content.generate_inspiration()["generated"])
            network.assert_not_called()
        self.assertEqual(self.content.status()["inspiration"], [])
        self.assertEqual(self.content.status()["health"], [])

    def test_user_card_validation_is_strict_while_feed_titles_are_safely_truncated(self):
        with self.assertRaises(ValueError):
            self.content.add_card({"title": "x" * 161})
        self.config["inspiration_sources"] = '["https://art.example/feed"]'
        rss = ('<rss><channel><item><title>' + "Very long artwork headline " * 20 + '</title><link>https://art.example/item</link><pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>').encode()
        with patch("flightwall.content._request", return_value=rss):
            self.content.refresh()
        card = self.content.status()["inspiration"][0]
        self.assertLessEqual(len(card["title"]), 160)
        self.assertTrue(card["title"].endswith("..."))
        self.assertEqual(card["source_url"], "https://art.example/item")
        self.assertEqual(card["published_at"], "2026-10-22T12:00:00+00:00")

    def test_atom_source_attribution_and_source_selection_revocation(self):
        self.config["inspiration_sources"] = '["https://design.example/atom"]'
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Warm rooms</title><link href="https://design.example/story"/><updated>2026-10-22T10:00:00-04:00</updated></entry></feed>'
        with patch("flightwall.content._request", return_value=atom):
            self.content.refresh()
        card = self.content.status()["inspiration"][0]
        self.assertEqual(card["source"], "design.example")
        self.assertEqual(card["published_at"], "2026-10-22T14:00:00+00:00")
        self.assertEqual(card["profile"], "lisa")
        self.config["inspiration_sources"] = "[]"
        self.assertEqual(self.content.status()["inspiration"], [])

    def test_feed_entity_declarations_rejected_and_expired_source_cards_hidden(self):
        self.config["inspiration_sources"] = '["https://art.example/feed"]'
        with patch("flightwall.content._request", return_value=b'<!DOCTYPE x [<!ENTITY private SYSTEM "file:///etc/passwd">]><rss/>'):
            self.content.refresh()
        self.assertTrue(self.content.status()["inspiration_error"])
        self.assertEqual(self.content.status()["inspiration"], [])
        selection = hashlib.sha256(self.config["inspiration_sources"].encode()).hexdigest()
        private_json(self.content.feeds_path, {"selection": selection, "cards": [{"title": "Old", "expires_at": (NOW - timedelta(seconds=1)).isoformat()}]})
        self.assertEqual(self.content.status()["inspiration"], [])

    def test_upload_reencodes_png_and_jpeg_without_metadata_and_preserves_private_modes(self):
        for format in ("PNG", "JPEG"):
            with self.subTest(format=format):
                saved = self.content.add_card(image_upload(format))
                path = self.root / "artwork" / (saved["id"] + ".png")
                with Image.open(path) as decoded:
                    self.assertEqual(decoded.format, "PNG")
                    self.assertEqual(decoded.mode, "RGB")
                    self.assertEqual(decoded.size, (512, 256))
                    self.assertFalse(decoded.getexif())
                    self.assertNotIn("Comment", decoded.info)
                self.assertNotIn(b"PRIVATE_DESCRIPTION", path.read_bytes())
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        if os.name == "posix":
            self.assertEqual((self.root / "artwork").stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.content.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_oversized_or_overpixel_upload_is_rejected_before_storage(self):
        for payload in (image_upload(image_data="data:image/svg+xml;base64,AAAA"), image_upload(image_data="data:image/png;base64,invalid!")):
            with self.assertRaises(ValueError):
                self.content.add_card(payload)
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.format, fake.width, fake.height = "PNG", 5000, 4000
        with patch("flightwall.content.Image.open", return_value=fake):
            with self.assertRaisesRegex(ValueError, "16 megapixels"):
                self.content.add_card(image_upload())
            fake.convert.assert_not_called()
        with patch("flightwall.content.base64.b64decode", return_value=b"x" * 5_000_001):
            with self.assertRaises(ValueError):
                self.content.add_card(image_upload())
        self.assertFalse((self.root / "artwork").exists())

    def test_board_capacity_failure_does_not_leave_orphan_uploaded_files(self):
        private_json(self.content.path, [{"id": str(index)} for index in range(100)])
        with self.assertRaisesRegex(ValueError, "100 saved cards"):
            self.content.add_card(image_upload())
        self.assertFalse((self.root / "artwork").exists())

    def test_artwork_path_traversal_is_refused(self):
        self.assertIsNone(self.content.image_data({"image_id": "../../secret"}))
        self.assertIsNone(self.content.image_data(None))

    def test_artwork_file_or_directory_symlinks_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("PRIVATE_FILE_CONTENT")
        artwork = self.root / "artwork"
        artwork.mkdir()
        try:
            (artwork / ("a" * 24 + ".png")).symlink_to(outside / "private.txt")
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable; Linux must run the symlink security checks")
            raise
        self.assertIsNone(self.content.image_data({"image_id": "a" * 24}))
        (artwork / ("a" * 24 + ".png")).unlink()
        artwork.rmdir()
        artwork.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "local directory"):
            self.content.add_card(image_upload())
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["private.txt"])

    def test_source_urls_refuse_private_ambiguous_hosts_credentials_and_invalid_ports(self):
        for url in ("http://example.com", "https://localhost/a", "https://127.0.0.1/a", "https://[::1]/a", "https://192.168.0.1/a",
                    "https://127.1/a", "https://0177.0.0.1/a", "https://0x7f000001/a", "https://2130706433/a",
                    "https://user:secret@example.com", "https://example.com:invalid/a", "https://printer.local/a"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _url(url)
        self.assertEqual(_url("https://example.com/source"), "https://example.com/source")

    def test_network_response_is_bounded_and_redirect_handler_drops_credentials(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * 101
        opener = MagicMock()
        opener.open.return_value = response
        with patch("flightwall.content.urllib.request.build_opener", return_value=opener) as build:
            with self.assertRaisesRegex(ValueError, "too large"):
                _request("https://api.example.com/source", token="private", max_bytes=100)
            self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_supported_source_dates_are_timezone_aware_and_ambiguous_dates_are_not_invented(self):
        self.assertEqual(_date("2026-10-22T12:30:00-04:00"), "2026-10-22T16:30:00+00:00")
        self.assertEqual(_date("Thu, 22 Oct 2026 16:30:00 GMT"), "2026-10-22T16:30:00+00:00")
        self.assertEqual(_date("2026-10-22"), "2026-10-22T00:00:00+00:00")
        self.assertEqual(_date("2026 Oct 22"), "2026-10-22T00:00:00+00:00")
        for value in (None, "yesterday", "2026 Oct", "2026-02-30", "2026-10-22T12:30:00"):
            self.assertIsNone(_date(value))

    def test_source_dated_research_preserves_attribution_without_generated_medical_claims(self):
        self.config.update(health_enabled=True, health_query="explicitly selected research topic")
        trial = {"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT12345678", "briefTitle": "Long study title " * 30},
                    "statusModule": {"lastUpdatePostDateStruct": {"date": "2026-10-21"}, "overallStatus": "RECRUITING"},
                    "designModule": {"phases": ["PHASE1"]}}}]}
        search = {"esearchresult": {"idlist": ["12345"]}}
        summary = {"result": {"12345": {"title": "Long publication headline " * 20, "pubdate": "2026 Oct 20", "source": "Example Journal"}}}
        with patch("flightwall.content._request", side_effect=[json.dumps(value).encode() for value in (trial, search, summary)]) as network:
            self.content._refresh_research(self.config)
        cards = self.content.status()["health"]
        self.assertEqual(len(cards), 2)
        self.assertEqual({card["source"] for card in cards}, {"PubMed", "ClinicalTrials.gov"})
        self.assertTrue(all(len(card["title"]) <= 160 for card in cards))
        self.assertTrue(all(card["published_at"] and card["fetched_at"] == NOW.isoformat() for card in cards))
        self.assertTrue(any("Study registration" in card["body"] for card in cards))
        self.assertTrue(any("Research publication" in card["body"] for card in cards))
        self.assertTrue(all("api.openai.com" not in call.args[0] for call in network.call_args_list))
        with patch("flightwall.content._now", return_value=NOW + timedelta(days=8)):
            self.assertEqual(self.content.status()["health"], [])
        self.config["health_query"] = "different explicit research topic"
        self.assertEqual(self.content.status()["health"], [])

    def test_future_dated_or_malformed_cached_research_cannot_be_shown_as_fresh(self):
        self.config.update(health_enabled=True, health_query="selected topic")
        selection = hashlib.sha256(self.config["health_query"].encode()).hexdigest()
        base = {"id": "n", "source": "PubMed", "source_url": "https://pubmed.ncbi.nlm.nih.gov/12345/", "published_at": NOW.isoformat(), "fetched_at": NOW.isoformat()}
        private_json(self.content.research_path, {"selection": selection, "cards": [base | {"published_at": (NOW + timedelta(days=1)).isoformat()},
                                                                                   base | {"source_url": "https://127.0.0.1/private"}, None]})
        self.assertEqual(self.content.status()["health"], [])
        private_json(self.content.research_path, {"selection": selection, "cards": {"malformed": True}})
        self.assertEqual(self.content.status()["health"], [])

    def enable_generation(self):
        self.config["openai_enabled"] = True
        (self.root / "openai.key").write_text("private-api-key")

    def test_generation_sends_only_explicit_interests_with_store_false_and_output_cap(self):
        self.enable_generation()
        self.config.update(health_query="PRIVATE_HEALTH_TOPIC", flight_roster="PRIVATE_CREW_ROSTER", messages="PRIVATE_MAIL", chris_location="PRIVATE_LOCATION")
        with patch("flightwall.content._request", return_value=openai_response()) as network:
            self.assertTrue(self.content.generate_inspiration()["generated"])
        call = network.call_args
        self.assertEqual(call.args[0], "https://api.openai.com/v1/responses")
        self.assertFalse(call.kwargs["payload"]["store"])
        self.assertEqual(call.kwargs["payload"]["input"], self.config["inspiration_topics"])
        self.assertLessEqual(call.kwargs["payload"]["max_output_tokens"], 1200)
        self.assertNotIn("PRIVATE_", json.dumps(call.kwargs["payload"]))
        self.assertEqual(call.kwargs["token"], "private-api-key")
        card = self.content.status()["inspiration"][0]
        self.assertTrue(card["ai_generated"])
        self.assertEqual(card["source"], "OpenAI / generated idea")
        self.assertEqual(card["kind"], "inspiration")
        self.assertEqual(self.content.status()["health"], [])
        if os.name == "posix":
            self.assertEqual((self.root / "openai-usage.json").stat().st_mode & 0o777, 0o600)

    def test_missing_key_and_corrupt_negative_or_boolean_counter_fail_before_billed_request(self):
        self.config["openai_enabled"] = True
        with patch("flightwall.content._request") as network:
            self.assertFalse(self.content.generate_inspiration()["generated"])
            network.assert_not_called()
        self.enable_generation()
        for usage in ("corrupt", {"date": NOW.date().isoformat(), "count": -1}, {"date": NOW.date().isoformat(), "count": False}, {"date": "invalid", "count": 0}):
            (self.root / "openai-usage.json").write_text(usage if isinstance(usage, str) else json.dumps(usage))
            with patch("flightwall.content._request") as network:
                self.assertFalse(self.content.generate_inspiration()["generated"])
                network.assert_not_called()

    def test_full_board_does_not_make_a_billed_generation_request(self):
        self.enable_generation()
        private_json(self.content.path, [{"id": str(index)} for index in range(100)])
        with patch("flightwall.content._request") as network:
            self.assertFalse(self.content.generate_inspiration()["generated"])
            network.assert_not_called()
        self.assertFalse((self.root / "openai-usage.json").exists())

    def test_twenty_generated_ideas_retain_latest_twelve_and_preserve_manual_choices(self):
        self.enable_generation()
        self.config["openai_daily_limit"] = 25
        first = self.content.add_card({"title": "A chosen manual idea", "body": "Keep this chosen text."})
        artwork = self.content.add_card(image_upload())
        with patch("flightwall.content._request", side_effect=[openai_response("Generated design exercise " + str(index)) for index in range(20)]) as network:
            for _index in range(20):
                self.assertTrue(self.content.generate_inspiration()["generated"])
        stored = json.loads(self.content.path.read_text())
        manual = [card for card in stored if card.get("ai_generated") is not True]
        generated = [card for card in stored if card.get("ai_generated") is True]
        self.assertEqual({card["id"] for card in manual}, {first["id"], artwork["id"]})
        self.assertEqual([card["body"] for card in generated], ["Generated design exercise " + str(index) for index in range(8, 20)])
        self.assertTrue((self.root / "artwork" / (artwork["id"] + ".png")).exists())
        self.assertEqual(network.call_count, 20)
        visible = self.content.status()["inspiration"]
        self.assertEqual(visible[0]["body"], "Generated design exercise 19")
        self.assertEqual(sum(card.get("ai_generated") is True for card in visible), 12)

    def test_full_mixed_board_rotates_generated_card_and_new_manual_choice_has_priority(self):
        self.enable_generation()
        manual = [{"id": "manual-" + str(index), "title": "Chosen " + str(index)} for index in range(99)]
        generated = {"id": "old-auto", "title": "Old generated idea", "ai_generated": True}
        private_json(self.content.path, manual + [generated])
        with patch("flightwall.content._request", return_value=openai_response()) as network:
            self.assertTrue(self.content.generate_inspiration()["generated"])
            self.assertEqual(network.call_count, 1)
        stored = json.loads(self.content.path.read_text())
        self.assertEqual(len(stored), 100)
        self.assertEqual(stored[:99], manual)
        self.assertNotIn(generated, stored)
        chosen = self.content.add_card({"title": "The final manual choice"})
        stored = json.loads(self.content.path.read_text())
        self.assertEqual(len(stored), 100)
        self.assertFalse(any(card.get("ai_generated") for card in stored))
        self.assertEqual(stored[-1]["id"], chosen["id"])
        with patch("flightwall.content._request") as network:
            self.assertFalse(self.content.generate_inspiration()["generated"])
            network.assert_not_called()

    def test_status_bounds_legacy_generated_cards_without_hiding_all_new_ideas(self):
        manual = [{"id": "manual-" + str(index), "title": "Chosen " + str(index)} for index in range(25)]
        generated = [{"id": "generated-" + str(index), "title": "Automatic " + str(index), "ai_generated": True,
                      "observed_at": NOW.isoformat()} for index in range(20)]
        private_json(self.content.path, manual + generated)
        visible = self.content.status()["inspiration"]
        self.assertEqual(len(visible), 20)
        self.assertEqual(sum(card.get("ai_generated") is True for card in visible), 12)
        self.assertEqual(visible[0]["id"], "generated-19")
        self.assertEqual(len(json.loads(self.content.path.read_text())), 45)  # Reading never deletes manual files.

    def test_generation_cap_is_shared_between_independent_instances_and_counts_failure(self):
        self.enable_generation()
        self.config["openai_daily_limit"] = 1
        other = SourceContent(self.root, lambda: deepcopy(self.config))
        with patch("flightwall.content._request", return_value=openai_response()) as network:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda content: content.generate_inspiration(), (self.content, other)))
        self.assertEqual(sum(value["generated"] for value in results), 1)
        self.assertEqual(network.call_count, 1)
        (self.root / "openai-usage.json").unlink()
        with patch("flightwall.content._request", side_effect=OSError("private-error")) as network:
            self.assertFalse(self.content.generate_inspiration()["generated"])
            self.assertFalse(self.content.generate_inspiration()["generated"])
            self.assertEqual(network.call_count, 1)

    def test_manual_layover_is_preserved_and_hotels_remain_explicitly_opt_in(self):
        payload = {"airport": "LAX", "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=12)).isoformat(), "hotel": "User-entered hotel", "city": "Los Angeles"}
        first = self.content.add_layover(payload)
        second = self.content.add_layover(payload | {"airport": "MEM", "city": "Memphis", "hotel": "Another user hotel"})
        items = self.content.layovers({"flights": 12, "layovers": {"malformed": True}})
        self.assertEqual({item["id"] for item in items}, {first["id"], second["id"]})
        self.assertTrue(all("hotel" not in item for item in items))
        self.config["hotel_details_enabled"] = True
        self.assertEqual(len([item for item in self.content.layovers({}) if item.get("hotel_user_provided")]), 2)


if __name__ == "__main__":
    unittest.main()
