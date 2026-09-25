"""loadtest/verify.py must pass on healthy data and fail on broken data."""
import importlib.util
from pathlib import Path

from sqlalchemy import update

from app.db import write_session
from app.models import Bill
from app.services import billing
from conftest import open_and_order, serve_all

spec = importlib.util.spec_from_file_location(
    "verify", Path(__file__).resolve().parent.parent / "loadtest" / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def _paid_order(db):
    kot = open_and_order(db, [("naan", 2), ("dal", 1)])
    serve_all(db, kot["order_id"])
    bill, _ = billing.generate_bill(kot["order_id"], 500, db["staff"]["counter"])
    billing.pay_bill(bill["bill_id"], "upi", db["staff"]["counter"])
    return bill


def test_verify_passes_on_healthy_data(db):
    _paid_order(db)
    open_and_order(db, [("lassi", 1)], table_index=1)  # still in progress: informational only
    verify.failures.clear()
    assert verify.main() == 0


def test_verify_catches_a_bill_that_does_not_match_its_lines(db):
    bill = _paid_order(db)
    with write_session() as s:  # simulate a bug: subtotal (and total) drift from the lines
        s.execute(update(Bill).where(Bill.id == bill["bill_id"])
                  .values(subtotal_paise=Bill.subtotal_paise + 100, total_paise=Bill.total_paise + 100))
    verify.failures.clear()
    assert verify.main() == 1
    assert any("subtotal" in f for f in verify.failures)
