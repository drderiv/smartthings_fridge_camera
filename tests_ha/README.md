# Tests against a real Home Assistant

`tests/` stubs Home Assistant, so it is fast and runs anywhere but cannot catch integration mistakes (wrong platform wiring, auth middleware, registry behaviour). These tests run the integration inside the same Home Assistant release the project targets, using `pytest-homeassistant-custom-component`.

```bash
docker build -t fridge-ha-tests -f tests_ha/Dockerfile .
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD":/repo -w /repo fridge-ha-tests \
    python -m pytest -c tests_ha/pytest.ini --rootdir=/repo tests_ha
```

They cover config/options/reauth flows, setup and unload, devices and entities, services, diagnostics, option-only reloads, and the signed image view going through Home Assistant's real authentication.
