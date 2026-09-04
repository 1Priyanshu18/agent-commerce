"""Manual, one-time completion of a Razorpay test-mode order via the hosted Checkout widget.

This is NOT part of any automated flow — it is a human-in-the-loop step you run yourself,
once, to produce a real recorded test-mode payment session for the demo. There is no fully
headless way to complete a Razorpay payment, even in test mode (confirmed by checking
Standard Checkout, Payment Links, and S2S integration docs — all three require a
browser-rendered step). This script must never be called from run_session.py, the eval loop,
or any test.

Usage:
    python scripts/live_test_checkout.py --amount 2000 --description "demo purchase"

Requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in your environment (.env). Always uses the
live_test adapter directly, regardless of the app's own PAYMENT_MODE setting.
"""

from __future__ import annotations

import argparse
import time
import webbrowser
from pathlib import Path

from agent_commerce.core.config import load_config
from agent_commerce.core.ids import generate_id
from agent_commerce.payments.live_test import RazorpayLiveTestAdapter
from agent_commerce.payments.models import PaymentStatus

_CHECKOUT_HTML = """<!DOCTYPE html>
<html>
<head><title>Live test checkout</title></head>
<body>
<p>Opening Razorpay test-mode Checkout for order {order_id}...</p>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
var options = {{
    "key": "{key_id}",
    "amount": "{amount_paise}",
    "currency": "INR",
    "order_id": "{order_id}",
    "name": "Agent Commerce (test mode)",
    "description": "{description}",
    "handler": function (response) {{
        document.body.innerHTML =
            "<h1>Payment submitted</h1><pre>" + JSON.stringify(response, null, 2) + "</pre>";
    }},
    "prefill": {{ "email": "test@example.com", "contact": "9999999999" }},
    "theme": {{ "color": "#3399cc" }}
}};
var rzp = new Razorpay(options);
rzp.open();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--amount", type=float, required=True, help="Amount in rupees, e.g. 2000 for Rs 2000"
    )
    parser.add_argument(
        "--description", default="Live test-mode checkout", help="Shown in the Checkout widget"
    )
    args = parser.parse_args()

    config = load_config()
    if not config.razorpay_key_id or not config.razorpay_key_secret:
        raise SystemExit("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set in your environment (.env)")

    adapter = RazorpayLiveTestAdapter(key_id=config.razorpay_key_id, key_secret=config.razorpay_key_secret)
    transaction_id = generate_id("txn_manual")
    amount_paise = round(args.amount * 100)

    order = adapter.create_order(
        transaction_id=transaction_id, amount_paise=amount_paise, policy_version="manual"
    )
    print(f"Created order {order.order_id} for Rs {args.amount:.2f} (receipt={order.receipt})")

    html = _CHECKOUT_HTML.format(
        key_id=config.razorpay_key_id,
        amount_paise=amount_paise,
        order_id=order.order_id,
        description=args.description,
    )
    html_path = Path(".cache") / "live_test_checkout.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    print(f"Opening {html_path} in your browser...")
    print("Use a test card, e.g. Visa 4100 2800 0000 1007, any future expiry date, any CVV.")
    webbrowser.open(html_path.resolve().as_uri())

    print("Waiting for the payment to be captured (Ctrl+C to stop waiting; the order stays open)...")
    try:
        while True:
            payments = adapter.fetch_payments(order.order_id)
            captured = [p for p in payments if p.status == PaymentStatus.CAPTURED]
            if captured:
                print(f"Captured: {captured[0].payment_id} (method={captured[0].method})")
                break
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nStopped waiting. The order remains open in Razorpay test mode.")

    print(
        f"\ntransaction_id={transaction_id} order_id={order.order_id}\n"
        "To see this reconciled, run the app with a cloudflared tunnel pointed at "
        "/webhooks/razorpay (see README) so the real webhook reaches it, or wait for the "
        "polling reconciler to pick it up on its next pass."
    )


if __name__ == "__main__":
    main()
