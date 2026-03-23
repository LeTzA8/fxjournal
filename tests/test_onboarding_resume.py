from models import TradeAccount, User, UserProfile, db


def _create_logged_in_user(client, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Main Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id

    return user


def test_skipped_onboarding_can_be_completed_later(app_ctx, client):
    user = _create_logged_in_user(
        client,
        username="onboarding-resume-user",
        email="onboarding-resume@example.com",
    )
    db.session.add(UserProfile(user_id=user.id, skipped=True))
    db.session.commit()

    get_response = client.get("/onboarding")
    post_response = client.post(
        "/onboarding",
        data={
            "trading_style": "intraday",
            "instruments": "mixed",
            "experience_level": "intermediate",
        },
        follow_redirects=False,
    )
    profile = UserProfile.query.filter_by(user_id=user.id).first()

    assert get_response.status_code == 200
    assert b"Question 1 - Trading style" in get_response.data
    assert post_response.status_code == 302
    assert profile is not None
    assert profile.skipped is False
    assert profile.completed_at is not None
    assert profile.trading_style == "intraday"
    assert profile.instruments == "mixed"
    assert profile.experience_level == "intermediate"
