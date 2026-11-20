import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_test_root = tempfile.TemporaryDirectory(prefix="vnc-test-import-")
os.environ["SMARTDATA_DB"] = str(Path(_test_root.name) / "import.db")
os.environ["SMARTDATA_ADMIN_EMAIL"] = "admin-test@vnc.local"
os.environ["SMARTDATA_ADMIN_PASSWORD"] = "AdminTestPass123!"
os.environ["SMARTDATA_ALLOW_DEMO_CREDIT"] = "1"
os.environ["SMARTDATA_ALLOW_DEMO_USAGE"] = "1"
os.environ["SMARTDATA_ALLOW_DEMO_WALLET"] = "1"
os.environ["INTERSWITCH_MODE"] = "TEST"

import app as backend


class FakeGatewayResponse:
    def __init__(self, value=None):
        self.value = value or {"ResponseCode": "00", "Amount": 30000}

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


def requery_payload(txn_ref, amount, response_code="00", merchant_reference=None):
    return {
        "ResponseCode": response_code,
        "Amount": amount,
        "MerchantReference": merchant_reference if merchant_reference is not None else txn_ref,
    }


class PaymentVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="vnc-payment-test-")
        backend.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        os.environ["INTERSWITCH_REQUERY_URL"] = "https://gateway.invalid/collections/api/v1/gettransaction.json"
        os.environ["INTERSWITCH_MERCHANT_CODE"] = "TEST-MERCHANT-UNIT-TEST"
        os.environ["INTERSWITCH_PAY_ITEM_ID"] = "TEST-PAY-ITEM-UNIT-TEST"
        backend.init_db()
        self.client = backend.app.test_client()
        registration = self.client.post(
            "/api/auth/register",
            json={
                "name": "Payment Test",
                "email": "payment-test@example.com",
                "password": "PaymentTest123!",
            },
        )
        self.assertEqual(registration.status_code, 200)
        self.headers = {
            "Authorization": f"Bearer {registration.get_json()['token']}"
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_verification_retry_does_not_call_gateway_or_double_credit(self):
        initiated = self.client.post(
            "/api/payments/initiate",
            headers=self.headers,
            json={"package": "daily"},
        )
        self.assertEqual(initiated.status_code, 200)
        intent = initiated.get_json()
        self.assertEqual(intent["merchant_code"], "TEST-MERCHANT-UNIT-TEST")
        self.assertEqual(intent["pay_item_id"], "TEST-PAY-ITEM-UNIT-TEST")
        txn_ref = intent["txn_ref"]

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(txn_ref, 30000)),
        ) as gateway:
            first = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": txn_ref},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["status"], "approved")
            gateway.assert_called_once_with(
                "https://gateway.invalid/collections/api/v1/gettransaction.json",
                params={
                    "merchantcode": "TEST-MERCHANT-UNIT-TEST",
                    "transactionreference": txn_ref,
                    "amount": 30000,
                },
                timeout=10,
            )

        with patch.object(
            backend.requests,
            "get",
            side_effect=AssertionError("verified payments should not call gateway again"),
        ) as gateway:
            retry = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": txn_ref},
            )
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.get_json()["status"], "approved")
            gateway.assert_not_called()

        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.get_json()["balance_gb"], 2)

    def test_declined_requery_does_not_credit_wallet(self):
        initiated = self.client.post(
            "/api/payments/initiate",
            headers=self.headers,
            json={"package": "daily"},
        )
        txn_ref = initiated.get_json()["txn_ref"]

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(txn_ref, 30000, "12")),
        ):
            response = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": txn_ref, "gateway_response": {"resp": "00"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "failed")
        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dashboard.get_json()["balance_gb"], 0)

        cancelled_intent = self.client.post(
            "/api/payments/initiate",
            headers=self.headers,
            json={"package": "daily"},
        ).get_json()
        cancelled_payload = requery_payload(cancelled_intent["txn_ref"], 30000)
        cancelled_payload["PaymentCancelled"] = True
        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(cancelled_payload),
        ):
            cancelled = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": cancelled_intent["txn_ref"]},
            )
        self.assertEqual(cancelled.get_json()["status"], "failed")
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["balance_gb"],
            0,
        )

    def test_demo_cash_seed_is_one_time_and_wallet_purchase_deducts_price(self):
        dashboard = self.client.get("/api/dashboard", headers=self.headers).get_json()
        self.assertEqual(dashboard["cash_wallet_balance_kobo"], 10_000_000)

        missing_key = self.client.post(
            "/api/wallet/purchase",
            headers=self.headers,
            json={"package": "test_500mb"},
        )
        self.assertEqual(missing_key.status_code, 400)
        idempotency_key = "wallet-purchase-test-1"
        purchase = self.client.post(
            "/api/wallet/purchase",
            headers=self.headers,
            json={"package": "test_500mb", "idempotency_key": idempotency_key},
        )
        self.assertEqual(purchase.status_code, 200)
        self.assertEqual(purchase.get_json()["gb_added"], 0.5)
        self.assertEqual(purchase.get_json()["cash_wallet_balance_kobo"], 9_999_000)

        retry = self.client.post(
            "/api/wallet/purchase",
            headers=self.headers,
            json={"package": "test_500mb", "idempotency_key": idempotency_key},
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["txn_ref"], purchase.get_json()["txn_ref"])

        key_reuse = self.client.post(
            "/api/wallet/purchase",
            headers=self.headers,
            json={"package": "daily", "idempotency_key": idempotency_key},
        )
        self.assertEqual(key_reuse.status_code, 409)

        backend.init_db()
        dashboard = self.client.get("/api/dashboard", headers=self.headers).get_json()
        self.assertEqual(dashboard["cash_wallet_balance_kobo"], 9_999_000)
        self.assertEqual(dashboard["balance_gb"], 0.5)
        self.assertEqual(dashboard["payments"][0]["mode"], "VNC_WALLET")

    def test_checkout_uses_existing_values_when_environment_is_unset(self):
        os.environ.pop("INTERSWITCH_MERCHANT_CODE", None)
        os.environ.pop("INTERSWITCH_PAY_ITEM_ID", None)
        response = self.client.post(
            "/api/wallet/topups/initiate",
            headers=self.headers,
            json={"amount_naira": 10},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["merchant_code"], "MX153376")
        self.assertEqual(response.get_json()["pay_item_id"], "9381029")
        dashboard = self.client.get("/api/dashboard", headers=self.headers).get_json()
        self.assertEqual(dashboard["payments"][0]["package_name"], "Wallet top-up")

    def test_no_default_admin_is_created_without_credentials(self):
        admin_email = os.environ.pop("SMARTDATA_ADMIN_EMAIL", None)
        admin_password = os.environ.pop("SMARTDATA_ADMIN_PASSWORD", None)
        original_db_path = backend.DB_PATH
        backend.DB_PATH = str(Path(self.temp_dir.name) / "no-admin.db")
        try:
            backend.init_db()
            con = backend.db()
            admin_count = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            user_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            con.close()
            self.assertEqual(admin_count, 0)
            self.assertEqual(user_count, 0)
        finally:
            backend.DB_PATH = original_db_path
            if admin_email is not None:
                os.environ["SMARTDATA_ADMIN_EMAIL"] = admin_email
            if admin_password is not None:
                os.environ["SMARTDATA_ADMIN_PASSWORD"] = admin_password

    def test_wallet_topup_requires_exact_server_verification(self):
        initial_balance = self.client.get(
            "/api/dashboard", headers=self.headers
        ).get_json()["cash_wallet_balance_kobo"]
        initiated = self.client.post(
            "/api/wallet/topups/initiate",
            headers=self.headers,
            json={"amount_naira": 10},
        )
        self.assertEqual(initiated.status_code, 200)
        intent = initiated.get_json()
        self.assertEqual(intent["amount_kobo"], 1000)

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(intent["txn_ref"], 1000.9)),
        ):
            fractional = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": intent["txn_ref"]},
            )
        self.assertEqual(fractional.get_json()["status"], "pending_verification")
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["cash_wallet_balance_kobo"],
            initial_balance,
        )

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(
                requery_payload(intent["txn_ref"], 1000, merchant_reference="OTHER-REFERENCE")
            ),
        ):
            reference_mismatch = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": intent["txn_ref"]},
            )
        self.assertEqual(reference_mismatch.get_json()["status"], "pending_verification")
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["cash_wallet_balance_kobo"],
            initial_balance,
        )

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(intent["txn_ref"], 1000)),
        ):
            approved = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": intent["txn_ref"]},
            )
        self.assertEqual(approved.get_json()["status"], "approved")
        self.assertEqual(approved.get_json()["wallet_credit_kobo"], 1000)
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["cash_wallet_balance_kobo"],
            initial_balance + 1000,
        )
        with patch.object(
            backend.requests,
            "get",
            side_effect=AssertionError("verified top-ups should not call gateway again"),
        ) as gateway:
            retry = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": intent["txn_ref"]},
            )
        self.assertEqual(retry.get_json()["status"], "approved")
        gateway.assert_not_called()
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["cash_wallet_balance_kobo"],
            initial_balance + 1000,
        )

    def test_ten_naira_plan_credits_only_when_full_amount_is_verified(self):
        plan = next(
            item
            for item in self.client.get("/api/plans").get_json()
            if item["key"] == "test_500mb"
        )
        self.assertEqual(plan["gb"], 0.5)
        self.assertEqual(plan["price_kobo"], 1000)

        initiated = self.client.post(
            "/api/payments/initiate",
            headers=self.headers,
            json={"package": "test_500mb"},
        )
        self.assertEqual(initiated.status_code, 200)
        intent = initiated.get_json()
        self.assertEqual(intent["amount_kobo"], 1000)
        txn_ref = intent["txn_ref"]

        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(txn_ref, 999)),
        ):
            mismatch = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": txn_ref},
            )

        self.assertEqual(mismatch.status_code, 200)
        self.assertEqual(mismatch.get_json()["status"], "failed")
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["balance_gb"],
            0,
        )

        second = self.client.post(
            "/api/payments/initiate",
            headers=self.headers,
            json={"package": "test_500mb"},
        )
        second_txn_ref = second.get_json()["txn_ref"]
        with patch.object(
            backend.requests,
            "get",
            return_value=FakeGatewayResponse(requery_payload(second_txn_ref, 1000)),
        ):
            approved = self.client.post(
                "/api/payments/verify",
                headers=self.headers,
                json={"txn_ref": second_txn_ref},
            )

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()["status"], "approved")
        self.assertEqual(approved.get_json()["gb_added"], 0.5)
        self.assertEqual(
            self.client.get("/api/dashboard", headers=self.headers).get_json()["balance_gb"],
            0.5,
        )
