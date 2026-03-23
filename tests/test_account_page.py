from models import TradeAccount, User, UserProfile, db


def _create_logged_in_user(client, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
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


def test_account_page_shows_onboarding_cta_and_drops_trade_count_overview(app_ctx, client):
    user = _create_logged_in_user(
        client,
        username="account-page-user",
        email="account-page@example.com",
    )
    db.session.add(UserProfile(user_id=user.id, skipped=True))
    db.session.commit()

    response = client.get("/account")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Trading Profile" in response_text
    assert "Save Trading Profile" in response_text
    assert "Select trading style" in response_text
    assert "Total Trades" not in response_text
    assert "Closed Trades" not in response_text
    assert "Running Trades" not in response_text


def test_account_page_shows_profile_hints_for_saved_values(app_ctx, client):
    user = _create_logged_in_user(
        client,
        username="account-profile-hints-user",
        email="account-profile-hints@example.com",
    )
    db.session.add(
        UserProfile(
            user_id=user.id,
            trading_style="intraday",
            instruments="mixed",
            experience_level="experienced",
            skipped=False,
        )
    )
    db.session.commit()

    response = client.get("/account")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Intraday" in response_text
    assert "Hours" in response_text
    assert "Mixed" in response_text
    assert "Multiple markets" in response_text
    assert "Experienced" in response_text
    assert "3+ years" in response_text


def test_account_page_can_update_trading_profile(app_ctx, client):
    user = _create_logged_in_user(
        client,
        username="account-profile-update-user",
        email="account-profile-update@example.com",
    )
    db.session.add(UserProfile(user_id=user.id, skipped=True))
    db.session.commit()

    response = client.post(
        "/account",
        data={
            "form_name": "trading_profile",
            "trading_style": "swing",
            "instruments": "gold",
            "experience_level": "intermediate",
        },
        follow_redirects=True,
    )
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Trading profile updated successfully." in response_text
    assert profile is not None
    assert profile.trading_style == "swing"
    assert profile.instruments == "gold"
    assert profile.experience_level == "intermediate"
    assert profile.skipped is False
    assert profile.completed_at is not None
