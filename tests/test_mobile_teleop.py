from pathlib import Path


def test_mobile_physical_teleop_uses_existing_owner_and_stop_contracts():
    viewer = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    html = (viewer / "index.html").read_text()
    js = (viewer / "viewer.js").read_text()
    css = (viewer / "viewer.css").read_text()
    assert 'data-tab="teleop"' in html
    assert 'id="physical-start"' in html
    assert 'id="physical-stop"' in html
    assert 'id="physical-phrase"' in html
    assert "post('/api/teleoperation/start', { confirmation:" in js
    assert "post('/api/teleoperation/stop')" in js
    assert "post('/api/mode/stop')" in js
    assert "el('physical-phrase').value !== 'START SOARM101'" in js
    assert "el('physical-confirm').checked = false" in js
    assert "state.teleop_preflight.length === 0" in js
    assert "Boolean(virtual?.lease)" in js
    assert "repeat(5, 1fr)" in css
    assert "body.host-native #teleop-pane { display: none !important; }" in css
