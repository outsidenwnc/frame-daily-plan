import config
import web


def client():
    return web.app.test_client()


def test_fresh_install_shows_setup():
    html = client().get('/').get_data(as_text=True)
    assert 'Getting started' in html and 'Search the network' in html


def test_language_toggle():
    c = client()
    c.get('/lang/de')
    assert 'Erste Schritte' in c.get('/').get_data(as_text=True)
    c.get('/lang/en')
    assert config.load()['ui_lang'] == 'en'


def test_set_place_fills_location_timezone_and_holidays():
    r = client().post('/set-place', data={'label': 'Munich, Bavaria, Germany', 'lat': '48.137', 'lon': '11.575',
                                         'timezone': 'Europe/Berlin', 'country_code': 'DE'})
    assert r.status_code == 302
    cfg = config.load()
    assert cfg['weather']['lat'] == 48.137 and cfg['timezone'] == 'Europe/Berlin' and cfg['holidays']['country'] == 'DE'


def test_regions_and_health():
    assert 'BY' in client().get('/regions/de').get_json()['regions']
    assert client().get('/health').get_json() == {'ok': True}


def test_bad_timezone_is_ignored():
    c = client()
    c.post('/set-place', data={'label': 'X', 'lat': '1', 'lon': '2', 'timezone': 'Not/AZone', 'country_code': ''})
    assert config.load()['timezone'] != 'Not/AZone'


def test_temperature_unit_setting():
    c = client()
    assert 'name="temp_unit" value="fahrenheit"' in c.get('/').get_data(as_text=True)
    form = {'tv_ip': '', 'timezone': 'America/New_York', 'start': '07:00', 'end': '10:00', 'weather_enabled': 'on',
            'temp_unit': 'fahrenheit', 'cal_count': '0'}
    c.post('/settings', data=form)
    assert config.load()['weather']['unit'] == 'fahrenheit'
    assert 'value="fahrenheit" checked' in c.get('/').get_data(as_text=True)
    c.post('/set-place', data={'label': 'Boston', 'lat': '42.36', 'lon': '-71.06', 'timezone': 'America/New_York'})
    assert config.load()['weather']['unit'] == 'fahrenheit'  # picking a place keeps the scale
    c.post('/settings', data=dict(form, temp_unit='kelvin'))
    assert config.load()['weather']['unit'] == 'celsius'      # unknown values fall back to Celsius
