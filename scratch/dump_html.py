import os
from flask import url_for
from cryptography.fernet import Fernet
from helpers.utils import encrypt_password
from models import MT5AccessRequest, MT5Account, TradeAccount, User, db
from app import app

def dump():
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
    os.environ["ADMIN_USER_EMAILS"] = "admin@example.com"
    # app is imported
    with app.app_context():
        # Setup tables
        db.create_all()
        
        user = User(
            username="mt5-request-states",
            email="mt5-request-states@example.com",
            password="hashed-password",
            email_verified=True,
            signup_status="approved",
        )
        db.session.add(user)
        db.session.flush()
        
        requestable_account = TradeAccount(
            user_id=user.id,
            name="Requestable CFD",
            account_type="CFD",
            is_default=True,
        )
        db.session.add(requestable_account)
        db.session.flush()
        
        pending_account = TradeAccount(
            user_id=user.id,
            name="Pending CFD",
            account_type="CFD",
            is_default=False,
        )
        approved_account = TradeAccount(
            user_id=user.id,
            name="Approved CFD",
            account_type="CFD",
            is_default=False,
        )
        linked_account = TradeAccount(
            user_id=user.id,
            name="Linked CFD",
            account_type="CFD",
            is_default=False,
        )
        submitted_account = TradeAccount(
            user_id=user.id,
            name="Submitted CFD",
            account_type="CFD",
            is_default=False,
        )
        db.session.add_all([pending_account, approved_account, linked_account, submitted_account])
        db.session.commit()
        
        db.session.add(
            MT5AccessRequest(
                user_id=user.id,
                trade_account_id=pending_account.id,
                status=MT5AccessRequest.STATUS_PENDING,
                request_note="Still waiting.",
            )
        )
        db.session.add(
            MT5AccessRequest(
                user_id=user.id,
                trade_account_id=approved_account.id,
                status=MT5AccessRequest.STATUS_APPROVED,
                request_note="Approved already.",
            )
        )
        db.session.add(
            MT5Account(
                user_id=user.id,
                trade_account_id=linked_account.id,
                account_number="99110002",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Server",
                is_active=True,
            )
        )
        db.session.add(
            MT5Account(
                user_id=user.id,
                trade_account_id=submitted_account.id,
                account_number="99110003",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Server",
                is_active=False,
            )
        )
        db.session.commit()
        
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user.id
            sess["username"] = user.username
            sess["display_timezone"] = "UTC"
            sess["active_trade_account_id"] = requestable_account.id
            
        resp = client.get("/dashboard")
        with open("scratch/debug_response.html", "w", encoding="utf-8") as f:
            f.write(resp.get_data(as_text=True))
        print("Dumped HTML response successfully.")

if __name__ == "__main__":
    dump()
