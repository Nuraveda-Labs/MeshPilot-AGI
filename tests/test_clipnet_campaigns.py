"""The campaign record every CLIPNET stage reads (spec § 3.3)."""
from meshpilot.agent.clipnet.campaigns import Campaign, load_campaign

ROW = {
    "slug": "lovable", "brand_ids": ["ai_empire"], "subject": "Lovable or its CEO Anton Osika",
    "required_hashtags": ["#LovablePartner"], "allowed_sources": ["youtube:9FGMhz-e97k"],
    "submit_platforms": ["instagram", "tiktok", "youtube"], "disclosure": "paid_promotion",
    "submit_window_min": 10, "max_clips_per_day": 5, "active": True,
}


def test_from_row_maps_every_field_to_immutable_types():
    c = Campaign.from_row(ROW)
    assert c.slug == "lovable"
    assert c.brand_ids == ("ai_empire",)
    assert c.submit_platforms == ("instagram", "tiktok", "youtube")
    assert c.submit_window_min == 10


def test_null_arrays_become_empty_tuples():
    c = Campaign.from_row({**ROW, "required_hashtags": None, "allowed_sources": None})
    assert c.required_hashtags == () and c.allowed_sources == ()


def test_allows_source_is_an_exact_match():
    c = Campaign.from_row(ROW)
    assert c.allows_source("youtube:9FGMhz-e97k")
    assert not c.allows_source("youtube:9FGMhz-e97")
    assert not c.allows_source("9FGMhz-e97k")


def test_an_empty_allow_list_allows_nothing():
    """Fail closed: a campaign with no provided sources must not accept any source."""
    assert not Campaign.from_row({**ROW, "allowed_sources": []}).allows_source("youtube:x")


def test_default_brand_only_when_unambiguous():
    assert Campaign.from_row(ROW).default_brand() == "ai_empire"
    assert Campaign.from_row({**ROW, "brand_ids": ["a", "b"]}).default_brand() is None


class _Result:
    def __init__(self, row): self._row = row
    def mappings(self): return self
    def first(self): return self._row


class _Conn:
    def __init__(self, row): self.row, self.params = row, None
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt, params):
        self.params = params
        return _Result(self.row)


class _Engine:
    def __init__(self, row): self.conn = _Conn(row)
    def connect(self): return self.conn


async def test_load_campaign_returns_the_record():
    eng = _Engine(ROW)
    c = await load_campaign("lovable", engine=eng)
    assert c is not None and c.slug == "lovable"
    assert eng.conn.params == {"slug": "lovable"}


async def test_load_campaign_missing_is_none():
    assert await load_campaign("nope", engine=_Engine(None)) is None


async def test_an_inactive_campaign_is_treated_as_absent():
    assert await load_campaign("lovable", engine=_Engine({**ROW, "active": False})) is None
