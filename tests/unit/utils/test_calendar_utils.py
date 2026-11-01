"""Testes de `rpa_3003.utils.calendar_utils` (RF-041, P-10, seção 8.13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import time_machine

from rpa_3003.core.exceptions import ConfigurationError, DataFormatError
from rpa_3003.utils import calendar_utils as cal

pytestmark = pytest.mark.unit

GOOD_FRIDAY_2026 = date(2026, 4, 3)
CARNIVAL_2026 = (date(2026, 2, 16), date(2026, 2, 17))


# --- conversão e formatação -------------------------------------------------------------------


def test_parse_date_acceptance_examples() -> None:
    """RF-041: parse_date("31/10/2026") == date(2026, 10, 31); "31-13-2026" é inválido."""
    assert cal.parse_date("31/10/2026") == date(2026, 10, 31)
    with pytest.raises(DataFormatError, match="Data inválida") as info:
        cal.parse_date("31-13-2026")
    assert info.value.details["valor"] == "31-13-2026"
    assert info.value.details["formatos"] == ", ".join(cal.PARSE_DATE_FORMATS)


@pytest.mark.parametrize(
    "text",
    [
        "31/10/2026",
        "2026-10-31",
        "31-10-2026",
        "31.10.2026",
        "20261031",
        "31/10/26",
        " 31/10/2026 ",
    ],
)
def test_parse_date_accepts_all_default_formats(text: str) -> None:
    """RF-041: cada máscara de PARSE_DATE_FORMATS é aceita (espaços nas pontas ignorados)."""
    assert cal.parse_date(text) == date(2026, 10, 31)


def test_parse_date_custom_formats() -> None:
    """RF-041: máscaras customizadas substituem as padrão."""
    assert cal.parse_date("10/31/2026", formats=["%m/%d/%Y"]) == date(2026, 10, 31)
    with pytest.raises(DataFormatError):
        cal.parse_date("31/10/2026", formats=["%m/%d/%Y"])


def test_parse_datetime_applies_timezone() -> None:
    """RF-041: parse_datetime retorna datetime com tzinfo=tz or DEFAULT_TZ."""
    parsed = cal.parse_datetime("31/10/2026 08:15")
    assert parsed == datetime(2026, 10, 31, 8, 15, tzinfo=cal.DEFAULT_TZ)
    assert parsed.tzinfo is cal.DEFAULT_TZ
    utc = ZoneInfo("UTC")
    assert cal.parse_datetime("2026-10-31T08:15:00", tz=utc).tzinfo is utc
    aware = cal.parse_datetime("2026-10-31 11:15:00+0000", formats=["%Y-%m-%d %H:%M:%S%z"])
    assert aware == datetime(2026, 10, 31, 8, 15, tzinfo=cal.DEFAULT_TZ)
    assert aware.tzinfo is cal.DEFAULT_TZ
    with pytest.raises(DataFormatError, match="Data e hora inválidas"):
        cal.parse_datetime("31/10/2026")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("31/10/2026", date(2026, 10, 31)),
        ("2026-10-31T23:59:00", date(2026, 10, 31)),
        (date(2026, 10, 31), date(2026, 10, 31)),
        (datetime(2026, 10, 31, 23, 59), date(2026, 10, 31)),
        (datetime(2026, 11, 1, 1, 0, tzinfo=UTC), date(2026, 10, 31)),
    ],
)
def test_to_date(value: cal.DateLike, expected: date) -> None:
    """RF-041: to_date aceita texto, date e datetime (com fuso convertido para DEFAULT_TZ)."""
    assert cal.to_date(value) == expected


def test_to_date_errors() -> None:
    """RF-041: texto inválido gera DataFormatError; tipo não suportado, TypeError."""
    with pytest.raises(DataFormatError):
        cal.to_date("ontem")
    with pytest.raises(TypeError, match="int"):
        cal.to_date(20261031)  # type: ignore[arg-type]


def test_format_date_and_long_format() -> None:
    """RF-041: format_date no padrão brasileiro; format_date_long por extenso."""
    assert cal.format_date(date(2026, 1, 5)) == "05/01/2026"
    moment = datetime(2026, 1, 5, 7, 8, 9)
    assert cal.format_date(moment, cal.DEFAULT_DATETIME_FORMAT) == "05/01/2026 07:08:09"
    assert cal.format_date_long(date(2026, 10, 31)) == "31 de outubro de 2026"
    assert cal.format_date_long(date(2026, 3, 1)) == "1 de março de 2026"


def test_month_and_weekday_names_do_not_use_locale() -> None:
    """RF-041: nomes de meses e dias em português, sem depender de locale."""
    assert [cal.month_name(month) for month in (1, 3, 12)] == ["janeiro", "março", "dezembro"]
    assert cal.weekday_name(date(2026, 10, 31)) == "sábado"
    assert cal.weekday_name(date(2026, 11, 2)) == "segunda-feira"
    for invalid in (0, 13):
        with pytest.raises(ValueError, match="Mês inválido"):
            cal.month_name(invalid)


# --- aritmética de datas ----------------------------------------------------------------------


def test_add_months_acceptance_example_and_edges() -> None:
    """RF-041: add_months(date(2026, 1, 31), 1) == date(2026, 2, 28); ajusta fim de mês."""
    assert cal.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert cal.add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert cal.add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)
    assert cal.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert cal.add_months(date(2026, 10, 31), 14) == date(2027, 12, 31)
    assert cal.add_months(datetime(2026, 5, 31, 10, 0), 1) == datetime(2026, 6, 30, 10, 0)


def test_add_days_and_month_boundaries() -> None:
    """RF-041: dias corridos e primeiro/último dia do mês."""
    assert cal.add_days(date(2026, 12, 31), 1) == date(2027, 1, 1)
    assert cal.add_days(date(2026, 3, 1), -1) == date(2026, 2, 28)
    assert cal.first_day_of_month(date(2026, 10, 31)) == date(2026, 10, 1)
    assert cal.last_day_of_month(date(2028, 2, 10)) == date(2028, 2, 29)
    assert cal.last_day_of_month(date(2026, 4, 1)) == date(2026, 4, 30)


def test_date_range() -> None:
    """RF-041: date_range inclui o fim, aceita passo positivo ou negativo e recusa zero."""
    assert list(cal.date_range(date(2026, 10, 30), date(2026, 11, 2))) == [
        date(2026, 10, 30),
        date(2026, 10, 31),
        date(2026, 11, 1),
        date(2026, 11, 2),
    ]
    assert list(cal.date_range(date(2026, 1, 1), date(2026, 1, 6), step_days=2)) == [
        date(2026, 1, 1),
        date(2026, 1, 3),
        date(2026, 1, 5),
    ]
    assert list(cal.date_range(date(2026, 1, 3), date(2026, 1, 1), step_days=-1)) == [
        date(2026, 1, 3),
        date(2026, 1, 2),
        date(2026, 1, 1),
    ]
    assert list(cal.date_range(date(2026, 1, 3), date(2026, 1, 1))) == []
    with pytest.raises(ValueError, match="zero"):
        cal.date_range(date(2026, 1, 1), date(2026, 1, 2), step_days=0)


# --- feriados e dias úteis --------------------------------------------------------------------


def test_add_business_days_skips_good_friday() -> None:
    """RF-041: add_business_days(date(2026, 4, 2), 1) == date(2026, 4, 6) (Sexta-feira Santa)."""
    assert cal.add_business_days(date(2026, 4, 2), 1) == date(2026, 4, 6)


def test_add_business_days_negative_and_zero() -> None:
    """RF-041: days negativo volta no calendário; days=0 retorna o dia útil ou o próximo."""
    assert cal.add_business_days(date(2026, 4, 6), -1) == date(2026, 4, 2)
    assert cal.add_business_days(date(2026, 11, 3), -1) == date(2026, 10, 30)  # 02/11 Finados
    assert cal.add_business_days(date(2026, 11, 3), -2) == date(2026, 10, 29)
    assert cal.add_business_days(GOOD_FRIDAY_2026, 0) == date(2026, 4, 6)
    assert cal.add_business_days(date(2026, 4, 2), 0) == date(2026, 4, 2)
    assert cal.add_business_days(datetime(2026, 4, 2, 18, 0), 1) == date(2026, 4, 6)


def test_carnival_only_with_optional_holidays() -> None:
    """RF-041: Carnaval só é feriado com include_optional=True."""
    national = cal.HolidayCalendar()
    optional = cal.HolidayCalendar(include_optional=True)
    for carnival in CARNIVAL_2026:
        assert national.is_holiday(carnival) is False
        assert optional.is_holiday(carnival) is True
        assert cal.is_business_day(carnival) is True
        assert cal.is_business_day(carnival, calendar=optional) is False
    # A categoria optional também inclui a Quarta-feira de Cinzas (18/02).
    assert cal.add_business_days(date(2026, 2, 13), 1, calendar=optional) == date(2026, 2, 19)
    assert cal.add_business_days(date(2026, 2, 13), 1) == date(2026, 2, 16)


def test_state_holidays_with_subdivision() -> None:
    """RF-041 e P-10: UF opcional inclui feriados estaduais (09/07 em SP)."""
    assert cal.HolidayCalendar(subdiv="SP").is_holiday(date(2026, 7, 9)) is True
    assert cal.HolidayCalendar().is_holiday(date(2026, 7, 9)) is False


def test_holiday_names_in_portuguese_regardless_of_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RF-041: nomes dos feriados no idioma do país, mesmo com LANG em inglês."""
    cal._library_holidays.cache_clear()
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LANGUAGE", "en_US")
    holidays_2026 = cal.HolidayCalendar().holidays(2026)
    assert holidays_2026[GOOD_FRIDAY_2026] == "Sexta-feira Santa"
    assert holidays_2026[date(2026, 11, 2)] == "Finados"
    assert list(holidays_2026) == sorted(holidays_2026)


def test_holidays_returns_copy_and_merges_extra_holidays() -> None:
    """RF-041: extra_holidays do ano entram no resultado; o cache não é alterado."""
    anniversary = date(2026, 1, 25)
    calendar = cal.HolidayCalendar(
        subdiv="SP", extra_holidays=frozenset({anniversary, date(2027, 1, 25), GOOD_FRIDAY_2026})
    )
    holidays_2026 = calendar.holidays(2026)
    assert holidays_2026[anniversary] == "Feriado adicional"
    assert holidays_2026[GOOD_FRIDAY_2026] == "Sexta-feira Santa"
    assert date(2027, 1, 25) not in holidays_2026
    assert calendar.is_holiday(anniversary) is True
    assert calendar.is_business_day(date(2026, 1, 26)) is True
    holidays_2026.clear()
    assert calendar.holidays(2026)
    assert cal.HolidayCalendar(subdiv="SP").is_holiday(anniversary) is False


def test_holiday_calendar_is_hashable_value_object() -> None:
    """RF-041: HolidayCalendar é imutável (dataclass frozen com slots)."""
    calendar = cal.HolidayCalendar(subdiv="SP")
    assert calendar == cal.HolidayCalendar(subdiv="SP")
    assert hash(calendar) == hash(cal.HolidayCalendar(subdiv="SP"))
    with pytest.raises(AttributeError):
        calendar.subdiv = "RJ"  # type: ignore[misc]


@pytest.mark.parametrize(
    "calendar",
    [
        cal.HolidayCalendar(country="ZZ"),
        cal.HolidayCalendar(subdiv="XX"),
        cal.HolidayCalendar(country="US", include_optional=True),
    ],
    ids=["pais", "subdivisao", "categoria"],
)
def test_unsupported_calendar_raises_configuration_error(calendar: cal.HolidayCalendar) -> None:
    """RF-041: país, UF ou categoria sem suporte geram ConfigurationError."""
    with pytest.raises(ConfigurationError, match="não suportado"):
        calendar.is_business_day(date(2026, 11, 3))


def test_is_business_day_weekends_holidays_and_datetimes() -> None:
    """RF-041: dia útil é segunda a sexta e não feriado; calendar=None usa HolidayCalendar()."""
    assert cal.is_business_day(date(2026, 11, 3)) is True
    assert cal.is_business_day(date(2026, 10, 31)) is False  # sábado
    assert cal.is_business_day(date(2026, 11, 1)) is False  # domingo
    assert cal.is_business_day(date(2026, 11, 2)) is False  # Finados
    assert cal.is_business_day(datetime(2026, 11, 3, 23, 0)) is True


def test_next_and_previous_business_day() -> None:
    """RF-041: próximo e anterior dia útil, com e sem include_today."""
    assert cal.next_business_day(date(2026, 10, 30)) == date(2026, 11, 3)
    assert cal.next_business_day(date(2026, 11, 3)) == date(2026, 11, 4)
    assert cal.next_business_day(date(2026, 11, 3), include_today=True) == date(2026, 11, 3)
    assert cal.previous_business_day(date(2026, 11, 3)) == date(2026, 10, 30)
    assert cal.previous_business_day(date(2026, 11, 2), include_today=True) == date(2026, 10, 30)
    assert cal.previous_business_day(date(2026, 10, 30), include_today=True) == date(2026, 10, 30)


def test_business_days_between_inclusive_exclusive_and_reversed() -> None:
    """RF-041: contagem inclusiva, exclusiva (sem start) e negativa com start > end."""
    tuesday, friday = date(2026, 11, 3), date(2026, 11, 6)
    assert cal.business_days_between(tuesday, friday) == 4
    assert cal.business_days_between(tuesday, friday, inclusive=False) == 3
    assert cal.business_days_between(friday, tuesday) == -4
    assert cal.business_days_between(friday, tuesday, inclusive=False) == -3
    assert cal.business_days_between(tuesday, tuesday) == 1
    assert cal.business_days_between(tuesday, tuesday, inclusive=False) == 0
    assert cal.business_days_between(date(2026, 11, 1), date(2026, 11, 30)) == 19


@pytest.mark.parametrize("days", [1, 5, 22])
def test_business_days_between_matches_add_business_days(days: int) -> None:
    """RF-041: inclusive=False é coerente com add_business_days."""
    start = date(2026, 3, 30)
    end = cal.add_business_days(start, days)
    assert cal.business_days_between(start, end, inclusive=False) == days


def test_nth_business_day() -> None:
    """RF-041: nth_business_day(2026, 11, 1) é o primeiro dia útil de novembro; n inválido."""
    assert cal.nth_business_day(2026, 11, 1) == date(2026, 11, 3)
    sp = cal.HolidayCalendar(subdiv="SP", include_optional=True)
    assert cal.nth_business_day(2026, 11, 5, calendar=sp) == date(2026, 11, 9)
    assert cal.nth_business_day(2026, 11, 19) == date(2026, 11, 30)
    with pytest.raises(ValueError, match="19 dias úteis"):
        cal.nth_business_day(2026, 11, 20)
    with pytest.raises(ValueError, match="maior ou igual a 1"):
        cal.nth_business_day(2026, 11, 0)


# --- data e hora atuais -----------------------------------------------------------------------


def test_now_and_today_use_default_timezone() -> None:
    """RF-041 e P-10: now() e today() usam America/Sao_Paulo por padrão."""
    instant = datetime(2026, 11, 1, 2, 30, tzinfo=UTC)
    with time_machine.travel(instant, tick=False):
        current = cal.now()
        assert current.tzinfo is cal.DEFAULT_TZ
        assert current == instant
        assert current.utcoffset() == timedelta(hours=-3)
        assert cal.today() == date(2026, 10, 31)
        assert cal.today(ZoneInfo("UTC")) == date(2026, 11, 1)


def test_usage_example() -> None:
    """Seção 8.13: exemplo de uso do módulo."""
    sp = cal.HolidayCalendar(subdiv="SP", include_optional=True)
    with time_machine.travel(datetime(2026, 11, 3, 12, 0, tzinfo=cal.DEFAULT_TZ), tick=False):
        data_ref = cal.previous_business_day(cal.today(), calendar=sp)
    vencimento = cal.add_business_days(cal.parse_date("30/10/2026"), 5, calendar=sp)
    quinto_dia_util = cal.nth_business_day(2026, 11, 5, calendar=sp)
    assert data_ref == date(2026, 10, 30)
    assert vencimento == date(2026, 11, 9)
    assert (
        cal.format_date(vencimento),
        cal.format_date_long(vencimento),
        cal.weekday_name(vencimento),
    ) == (
        "09/11/2026",
        "9 de novembro de 2026",
        "segunda-feira",
    )
    assert quinto_dia_util == date(2026, 11, 9)
