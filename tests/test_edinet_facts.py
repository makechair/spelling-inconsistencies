"""EDINET XBRL instance parsing.

api.edinet-fsa.go.jp is unreachable from development, so these instances are
built from the documented XBRL structure. They pin the decisions that hold
whatever a real filing looks like -- above all which contexts count, since
reading the wrong ones inflates every total rather than failing visibly.
"""

from __future__ import annotations

from datetime import date

from usstocks.corpus.edinet_facts import (
    describe_archive,
    normalize_instance,
    parse_contexts,
    parse_units,
)

INSTANCE = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
    xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period>
      <xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2026-03-31</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration_SemiconductorReportableSegmentsMember">
    <xbrli:period>
      <xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2026-03-31</xbrli:endDate>
    </xbrli:period>
    <xbrli:scenario>
      <xbrldi:explicitMember dimension="jppfs_cor:SegmentAxis">seg:Semi</xbrldi:explicitMember>
    </xbrli:scenario>
  </xbrli:context>
  <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
  <xbrli:unit id="JPYPerShares">
    <xbrli:divide>
      <xbrli:unitNumerator><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unitNumerator>
      <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
    </xbrli:divide>
  </xbrli:unit>

  <jppfs_cor:NetSales contextRef="CurrentYearDuration"
      unitRef="JPY">2400000000000</jppfs_cor:NetSales>
  <jppfs_cor:NetSales contextRef="CurrentYearDuration_SemiconductorReportableSegmentsMember"
      unitRef="JPY">900000000000</jppfs_cor:NetSales>
  <jppfs_cor:CostOfSales contextRef="CurrentYearDuration"
      unitRef="JPY">1300000000000</jppfs_cor:CostOfSales>
  <jppfs_cor:Inventories contextRef="CurrentYearInstant"
      unitRef="JPY">400000000000</jppfs_cor:Inventories>
  <jppfs_cor:Assets contextRef="CurrentYearInstant"
      unitRef="JPY">3800000000000</jppfs_cor:Assets>
  <jpcrp_cor:CompanyName
      contextRef="CurrentYearInstant">東京エレクトロン</jpcrp_cor:CompanyName>
</xbrli:xbrl>
"""


def rows_of(text: str = INSTANCE):
    return normalize_instance(
        "8035", text.encode("utf-8"),
        doc_id="S100ABCD", form="20-F", filed=date(2026, 6, 25),
    )


def test_segment_contexts_are_excluded():
    """A filer tags consolidated revenue and each segment's revenue with the
    same concept. Reading both would report sales the company never made."""
    revenue = [row for row in rows_of() if row["concept"] == "revenue"]
    assert len(revenue) == 1
    assert revenue[0]["value"] == 2_400_000_000_000.0


def test_contexts_with_dimensions_never_reach_the_parser():
    root_contexts = parse_contexts(
        __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(INSTANCE)
    )
    assert set(root_contexts) == {"CurrentYearDuration", "CurrentYearInstant"}


def test_balance_and_flow_facts_keep_their_shapes():
    by_concept = {row["concept"]: row for row in rows_of()}
    assert by_concept["revenue"]["period_start"] == date(2025, 4, 1)
    assert by_concept["inventory"]["period_start"] is None
    assert by_concept["inventory"]["period_end"] == date(2026, 3, 31)
    assert by_concept["assets"]["value"] == 3_800_000_000_000.0


def test_the_reporting_currency_is_read_from_the_unit():
    assert {row["unit"] for row in rows_of()} == {"JPY"}


def test_per_share_units_are_not_treated_as_levels():
    units = parse_units(
        __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(INSTANCE)
    )
    assert units == {"JPY": "JPY"}


def test_narrative_taxonomies_are_ignored():
    """jpcrp carries the cover page and prose, not figures."""
    assert all(row["concept"] in {"revenue", "cost_of_revenue", "inventory", "assets"}
               for row in rows_of())


def test_the_document_id_travels_as_the_accession():
    assert {row["accession"] for row in rows_of()} == {"S100ABCD"}
    assert {row["form"] for row in rows_of()} == {"20-F"}


def test_an_ifrs_filer_is_read_through_the_same_concepts():
    ifrs = INSTANCE.replace(
        "jppfs_cor:NetSales", "jpigp_cor:RevenueIFRS"
    ).replace(
        'xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"',
        'xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"\n'
        '    xmlns:jpigp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor"',
    )
    revenue = [row for row in rows_of(ifrs) if row["concept"] == "revenue"]
    assert len(revenue) == 1
    assert revenue[0]["xbrl_tag"] == "RevenueIFRS"


def test_describe_lists_what_a_filing_holds(tmp_path):
    """Development cannot reach EDINET, so an unmatched filing has to name its
    own concepts rather than costing another round trip."""
    import zipfile

    archive = tmp_path / "xbrl.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/jpcrp-asr.xbrl", INSTANCE)
    described = describe_archive(archive)
    assert "NetSales" in described
    assert "CompanyName" not in described  # jpcrp is not a financial taxonomy


def test_the_company_name_comes_from_the_universe_file():
    """EDINET identifies a filer by a four-digit code. The instance does carry
    a CompanyName, but it sits in jpcrp, which this parser ignores on purpose
    -- so the name has to come from the universe entry instead."""
    rows = normalize_instance(
        "8035", INSTANCE.encode("utf-8"),
        doc_id="S100ABCD", form="20-F", filed=date(2026, 6, 25),
        entity_name="東京エレクトロン",
    )
    assert {row["entity_name"] for row in rows} == {"東京エレクトロン"}
