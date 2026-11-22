import datetime
import io
import json
import os
import zoneinfo

import icalendar
from PIL import Image

import jobs
import plan_data
import render

HERE = os.path.dirname(__file__)
TZ = zoneinfo.ZoneInfo('Europe/London')
CFG = {'timezone': 'Europe/London', 'plan_lang': 'en', 'calendars': [], 'dim_past_events': True,
       'weather': {'enabled': False}, 'holidays': {'country': '', 'subdiv': ''}, 'background': 'default',
       'tv_ip': '192.168.1.50', 'schedule': {'enabled': True, 'start': '07:00', 'end': '10:00', 'days': [0, 1, 2, 3, 4]}}


def cal():
    with open(os.path.join(HERE, 'sample.ics'), 'rb') as f:
        return icalendar.Calendar.from_ical(f.read())


def test_calendar_entries():
    events, todos = plan_data.calendar_entries(cal(), 'green', datetime.date(2026, 10, 27), TZ, plan_data.WORDS['en'])
    titles = [(e['start'], e['title']) for e in sorted(events, key=lambda e: e['sort'])]
    assert ('All day', 'Project week') in titles
    assert ('07:15', 'Breakfast together') in titles  # whitespace collapsed
    assert ('16:00', 'Piano lesson') in titles      # weekly recurrence expanded
    assert todos == ['Tidy up your room']           # "To-do:" prefix moves it to the list


def test_todo_calendar_takes_every_entry():
    _, todos = plan_data.calendar_entries(cal(), 'orange', datetime.date(2026, 10, 27), TZ, plan_data.WORDS['en'], True)
    assert len(todos) == 4


def test_holidays_in_plan_language():
    cfg = dict(CFG, holidays={'country': 'DE', 'subdiv': 'BY'})
    xmas = datetime.date(2026, 12, 25)
    en = plan_data.holiday_entries(dict(cfg, plan_lang='en'), xmas, plan_data.WORDS['en'])
    de = plan_data.holiday_entries(dict(cfg, plan_lang='de'), xmas, plan_data.WORDS['de'])
    assert en[0]['start'] == 'Holiday' and 'Christmas' in en[0]['title']
    assert de[0]['start'] == 'Feiertag' and 'Weihnacht' in de[0]['title']


def fake_forecast(monkeypatch, tmax, tmin, code=2, rain=10):
    body = json.dumps({'daily': {'weather_code': [code], 'temperature_2m_max': [tmax],
                                 'temperature_2m_min': [tmin], 'precipitation_probability_max': [rain]}})
    monkeypatch.setattr(plan_data.urllib.request, 'urlopen', lambda *a, **k: io.StringIO(body))


def test_weather_celsius_is_default(monkeypatch):
    fake_forecast(monkeypatch, 19.4, 11.6)
    cfg = dict(CFG, weather={'enabled': True, 'lat': 1, 'lon': 2})
    wx = plan_data.weather(cfg, datetime.date(2026, 10, 27), plan_data.WORDS['en'])
    assert (wx['max'], wx['min'], wx['unit']) == (19, 12, 'C')


def test_weather_fahrenheit_keeps_celsius_hints(monkeypatch):
    fake_forecast(monkeypatch, 10.0, 2.0)  # 50 °F / 36 °F: still "cold" by the °C thresholds
    cfg = dict(CFG, weather={'enabled': True, 'lat': 1, 'lon': 2, 'unit': 'fahrenheit'})
    wx = plan_data.weather(cfg, datetime.date(2026, 10, 27), plan_data.WORDS['en'])
    assert (wx['max'], wx['min'], wx['unit']) == (50, 36, 'F')
    assert wx['hint'] == plan_data.WORDS['en']['hint_cold']


def test_schedule_rules():
    d = lambda h, m=0, day=27: datetime.datetime(2026, 10, day, h, m, tzinfo=TZ)  # 27th = Tuesday
    today = '2026-10-27'
    assert jobs._due(CFG, {}, d(6, 59)) is None
    assert jobs._due(CFG, {}, d(7)) == 'show'
    assert jobs._due(CFG, {'shown_on': today, 'restore_due': True}, d(8)) is None
    assert jobs._due(CFG, {'shown_on': today, 'restore_due': True}, d(10)) == 'restore'
    assert jobs._due(CFG, {'shown_on': today, 'restore_due': False}, d(15)) is None  # manual message stays
    assert jobs._due(CFG, {'restore_due': True}, d(6, day=28)) == 'restore'           # TV was off at 10
    assert jobs._due(CFG, {}, d(8, day=25)) is None                                   # Sunday not scheduled
    assert jobs._due(dict(CFG, tv_ip=''), {}, d(8)) is None                          # not set up yet


def test_render_both_languages(tmp_path):
    now = datetime.datetime(2026, 10, 27, 8, 0, tzinfo=TZ)
    for lang in ('en', 'de'):
        out = tmp_path / f'demo-{lang}.jpg'
        render.render(plan_data.demo(CFG, now, lang), str(out))
        assert Image.open(out).size == (3840, 2160)


def test_render_in_fahrenheit(tmp_path):
    now = datetime.datetime(2026, 10, 27, 8, 0, tzinfo=TZ)
    data = plan_data.demo(dict(CFG, weather={'unit': 'fahrenheit'}), now, 'en')
    assert (data['weather']['max'], data['weather']['min'], data['weather']['unit']) == (66, 54, 'F')
    render.render(data, str(tmp_path / 'f.jpg'))
    assert render.degrees(66, 'F') == '66°F' and render.degrees(19, None) == '19°'


def test_text_and_fit(tmp_path):
    render.render_text('Grandma picks you up at 4 pm today', str(tmp_path / 't.jpg'))
    Image.new('RGB', (600, 1200), (200, 100, 50)).save(tmp_path / 'tall.jpg')
    for mode in ('crop', 'fit'):
        render.fit_artwork(str(tmp_path / 'tall.jpg'), str(tmp_path / f'{mode}.jpg'), mode)
        assert Image.open(tmp_path / f'{mode}.jpg').size == (3840, 2160)
