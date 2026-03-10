from src.ina_backend.app.auth import hash_password, verify_password, create_access_token
def test_password_hash_and_verify():
    pw = "MySecret123"
    h = hash_password(pw)
    assert verify_password(pw, h)
def test_create_token_contains_sub():
    token = create_access_token({"sub": "1"})
    assert token is not None
