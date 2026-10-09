"""Self-authored synthetic production/account evidence; never customer data."""
def account_evidence():
    return {"schema": "integrity-official-account-evidence/v1", "account_alias": "synthetic-account",
            "expected_model": "gpt-6-astra", "authorization": {"authorized": True,
            "basis": "self_authored", "source": "synthetic_regression_fixture"},
            "provenance": {"source": "official_account", "trusted": True, "complete": True},
            "events_coverage_complete": False, "events": [], "catalog": []}
