from app.traffic import request_payload


def test_every_fifth_synthetic_request_uses_a_digital_wallet() -> None:
    methods = [request_payload(index)["payment_method"] for index in range(1, 11)]

    assert methods.count("digital_wallet") == 2
    assert methods[4] == "digital_wallet"
    assert methods[9] == "digital_wallet"
