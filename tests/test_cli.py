import json
from retailpulse.cli import main


def test_build_writes_dashboard(tmp_path):
    rc = main(["build", "--out", str(tmp_path), "--orders", "1500", "--seed", "3"])
    assert rc == 0
    html = (tmp_path / "index.html").read_text()
    assert "<svg" in html and "RetailPulse" in html and "Show SQL" in html
    data = json.loads((tmp_path / "data.json").read_text())
    assert data["meta"]["orders"] == 1500
    assert set(data["results"]) == {f"0{i}_" + n for i, n in enumerate(
        ["monthly_revenue", "category_abc", "customer_rfm", "cohort_retention",
         "store_performance", "channel_mix", "repeat_purchase_rate", "data_quality"], start=1)}
