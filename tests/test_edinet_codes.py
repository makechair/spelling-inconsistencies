"""The mirrored EDINET filer list and the search over it.

disclosure2dl.edinet-fsa.go.jp is unreachable from development, so the archive
here is built to the published shape: a title line, then a header, then rows
in cp932.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from usstocks.corpus.daily import CorpusError
from usstocks.corpus.edinet_codes import parse_code_list, search, write_companies

HEADER = (
    '"ＥＤＩＮＥＴコード","提出者種別","上場区分","提出者名","証券コード","提出者業種"'
)
ROWS = [
    '"E01234","内国法人","上場","株式会社日立製作所","65010","電気機器"',
    '"E05678","内国法人","上場","東京エレクトロン株式会社","80350","電気機器"',
    # Unlisted: no securities code, nothing to chart, and thousands of them.
    '"E09999","内国法人","非上場","たとえば投資法人","","その他"',
    # A code issued since 2024, with a letter in the fourth position.
    '"E01111","内国法人","上場","新規上場株式会社","130A0","サービス業"',
]


def archive(lines=None) -> bytes:
    body = "\n".join(["EDINETコードリスト,2026-08-06", HEADER, *(lines or ROWS)])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("EdinetcodeDlInfo.csv", body.encode("cp932"))
    return buffer.getvalue()


def test_only_listed_companies_survive():
    """Roughly twenty thousand filers exist; the few thousand with a
    securities code are the ones this project can chart."""
    companies = parse_code_list(archive())
    assert [company["code"] for company in companies] == ["130A", "6501", "8035"]


def test_the_five_character_code_is_narrowed():
    """EDINET writes 6501 as 65010, including for the codes ending in a
    letter, where demanding all digits would drop the newest listings."""
    codes = {company["name"]: company["code"] for company in parse_code_list(archive())}
    assert codes["株式会社日立製作所"] == "6501"
    assert codes["新規上場株式会社"] == "130A"


def test_the_industry_travels_for_context():
    company = next(c for c in parse_code_list(archive()) if c["code"] == "6501")
    assert company["industry"] == "電気機器"
    assert company["edinet_code"] == "E01234"


def test_a_header_that_never_appears_is_an_error():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("EdinetcodeDlInfo.csv", "no,header,here\n1,2,3".encode("cp932"))
    with pytest.raises(CorpusError, match="header"):
        parse_code_list(buffer.getvalue())


def test_something_that_is_not_a_zip_is_an_error():
    with pytest.raises(CorpusError, match="ZIP"):
        parse_code_list(b"<html>maintenance</html>")


def test_an_exact_code_outranks_a_name_that_contains_it():
    """Typing 6501 must not answer with a company whose name happens to hold
    those digits."""
    companies = [
        {"code": "1234", "name": "6501記念ホールディングス"},
        {"code": "6501", "name": "株式会社日立製作所"},
    ]
    assert [c["code"] for c in search(companies, "6501")] == ["6501", "1234"]


def test_a_name_prefix_outranks_a_name_fragment():
    companies = [
        # Japanese filers put 株式会社 on either side of the name, so a search
        # for the name itself has to reach the ones that lead with the suffix
        # -- but behind the ones that lead with the name.
        {"code": "0001", "name": "株式会社ソニーグループ販売"},
        {"code": "0002", "name": "ソニーグループ株式会社"},
    ]
    assert [c["code"] for c in search(companies, "ソニーグループ")] == ["0002", "0001"]


def test_an_empty_query_matches_nothing():
    """Otherwise the first keystroke's worth of debounce would return the
    whole list."""
    assert search([{"code": "6501", "name": "日立"}], "   ") == []


def test_the_written_file_carries_what_the_api_reads(tmp_path: Path):
    import json

    path = tmp_path / "companies.json"
    write_companies(path, parse_code_list(archive()))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert {c["code"] for c in payload["companies"]} == {"130A", "6501", "8035"}
