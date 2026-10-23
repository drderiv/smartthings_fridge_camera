# Test image: the Home Assistant release the integration is developed against, plus the
# pytest-homeassistant-custom-component plugin.
#   docker build -t fridge-ha-tests -f tests_ha/Dockerfile .
#   docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/repo -w /repo \
#       fridge-ha-tests python -m pytest -c tests_ha/pytest.ini --rootdir=/repo tests_ha
FROM ghcr.io/home-assistant/home-assistant:stable
RUN pip install --no-cache-dir pytest-homeassistant-custom-component
ENTRYPOINT []
