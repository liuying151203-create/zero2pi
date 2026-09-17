from zero2pi.main import main


def test_environment_smoke(capsys) -> None:
    main()
    assert capsys.readouterr().out.strip() == "zero2pi environment is ready"
