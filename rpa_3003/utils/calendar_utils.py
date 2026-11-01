"""Datas e dias úteis.

Datas e horas no fuso padrão, conversão e formatação no padrão brasileiro e cálculo de dias
úteis com feriados.

O fuso padrão é `America/Sao_Paulo` (P-10), com a base IANA do pacote `tzdata` no Windows. Os
nomes de meses e dias vêm de `MONTHS_PT` e `WEEKDAYS_PT`, nunca de `locale`. Os feriados vêm da
biblioteca `holidays`, importada apenas no primeiro cálculo.
"""

from __future__ import annotations

import functools
from calendar import monthrange
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, TypeAlias
from zoneinfo import ZoneInfo

from rpa_3003.core.exceptions import ConfigurationError, DataFormatError

__all__: list[str] = [
    "DEFAULT_DATETIME_FORMAT",
    "DEFAULT_DATE_FORMAT",
    "DEFAULT_TZ",
    "MONTHS_PT",
    "PARSE_DATETIME_FORMATS",
    "PARSE_DATE_FORMATS",
    "WEEKDAYS_PT",
    "DateLike",
    "HolidayCalendar",
    "add_business_days",
    "add_days",
    "add_months",
    "business_days_between",
    "date_range",
    "first_day_of_month",
    "format_date",
    "format_date_long",
    "is_business_day",
    "last_day_of_month",
    "month_name",
    "next_business_day",
    "now",
    "nth_business_day",
    "parse_date",
    "parse_datetime",
    "previous_business_day",
    "to_date",
    "today",
    "weekday_name",
]

DEFAULT_TZ: Final[ZoneInfo] = ZoneInfo("America/Sao_Paulo")
DEFAULT_DATE_FORMAT: Final[str] = "%d/%m/%Y"
DEFAULT_DATETIME_FORMAT: Final[str] = "%d/%m/%Y %H:%M:%S"
PARSE_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y%m%d",
    "%d/%m/%y",
)
PARSE_DATETIME_FORMATS: Final[tuple[str, ...]] = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
MONTHS_PT: Final[tuple[str, ...]] = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
WEEKDAYS_PT: Final[tuple[str, ...]] = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
DateLike: TypeAlias = date | datetime | str  # noqa: UP040 - convenção 7.7 usa TypeAlias

_SATURDAY: Final[int] = 5
_ONE_DAY: Final[timedelta] = timedelta(days=1)
_PUBLIC_CATEGORY: Final[str] = "public"
_OPTIONAL_CATEGORY: Final[str] = "optional"
_EXTRA_HOLIDAY_NAME: Final[str] = "Feriado adicional"


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    """Calendário de feriados para o cálculo de dias úteis.

    Attributes:
        country: Código ISO do país na biblioteca `holidays`.
        subdiv: Subdivisão (UF no Brasil, ex.: `"SP"`); `None` usa só feriados nacionais.
        include_optional: Inclui a categoria `optional` da biblioteca (ex.: Carnaval).
        extra_holidays: Datas adicionais sem expediente (ex.: feriados municipais).

    Example:
        >>> HolidayCalendar().is_holiday(date(2026, 4, 3))  # Sexta-feira Santa
        True
    """

    country: str = "BR"
    subdiv: str | None = None
    include_optional: bool = False
    extra_holidays: frozenset[date] = field(default_factory=frozenset)

    def holidays(self, year: int) -> dict[date, str]:
        """Retorna os feriados do ano, ordenados por data, com nomes no idioma do país.

        Args:
            year: Ano de referência.

        Returns:
            Dicionário data → nome (cópia; alterá-lo não afeta o cache).

        Raises:
            ConfigurationError: Se país, subdivisão ou categoria não forem suportados pela
                biblioteca `holidays`.
        """
        names = dict(_library_holidays(self.country, self.subdiv, self.include_optional, year))
        for extra in self.extra_holidays:
            if extra.year == year:
                names.setdefault(extra, _EXTRA_HOLIDAY_NAME)
        return dict(sorted(names.items()))

    def is_holiday(self, value: date) -> bool:
        """Indica se a data é feriado neste calendário.

        Args:
            value: Data; `datetime` é reduzido à data.

        Returns:
            `True` para feriados da biblioteca e de `extra_holidays`.

        Raises:
            ConfigurationError: Se o calendário não for suportado pela biblioteca `holidays`.
        """
        day = _as_date(value)
        if day in self.extra_holidays:
            return True
        return day in _library_holidays(self.country, self.subdiv, self.include_optional, day.year)

    def is_business_day(self, value: date) -> bool:
        """Indica se a data é dia útil: segunda a sexta e não feriado.

        Args:
            value: Data; `datetime` é reduzido à data.

        Returns:
            `True` para dias úteis.

        Raises:
            ConfigurationError: Se o calendário não for suportado pela biblioteca `holidays`.
        """
        day = _as_date(value)
        return day.weekday() < _SATURDAY and not self.is_holiday(day)


_DEFAULT_CALENDAR: Final[HolidayCalendar] = HolidayCalendar()


def now(tz: ZoneInfo | None = None) -> datetime:
    """Data e hora atuais com fuso.

    Args:
        tz: Fuso; `None` usa `DEFAULT_TZ`.

    Returns:
        `datetime` com `tzinfo`.
    """
    return datetime.now(tz or DEFAULT_TZ)


def today(tz: ZoneInfo | None = None) -> date:
    """Data atual no fuso informado.

    Args:
        tz: Fuso; `None` usa `DEFAULT_TZ`.

    Returns:
        Data de hoje.
    """
    return now(tz).date()


def to_date(value: DateLike) -> date:
    """Converte `date`, `datetime` ou texto em `date`.

    `datetime` com fuso é convertido para `DEFAULT_TZ` antes de extrair a data; texto é
    interpretado por `parse_date` e, se não casar, por `parse_datetime`.

    Args:
        value: Valor a converter.

    Returns:
        A data correspondente.

    Raises:
        DataFormatError: Se o texto não casar com nenhuma máscara.
        TypeError: Se o tipo não for suportado.
    """
    if isinstance(value, datetime):
        return _as_date(value)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return parse_date(value)
        except DataFormatError:
            return parse_datetime(value).date()
    raise TypeError(f"Tipo de data não suportado: {type(value).__name__}")


def parse_date(value: str, formats: Sequence[str] = PARSE_DATE_FORMATS) -> date:
    """Interpreta um texto como data usando a primeira máscara compatível.

    Args:
        value: Texto (espaços nas pontas são ignorados).
        formats: Máscaras `strptime`, na ordem de tentativa.

    Returns:
        A data interpretada.

    Raises:
        DataFormatError: Se nenhuma máscara casar, com o valor e as máscaras em `details`.

    Example:
        >>> parse_date("31/10/2026")
        datetime.date(2026, 10, 31)
    """
    return _parse(value, formats, "Data inválida").date()


def parse_datetime(
    value: str, formats: Sequence[str] = PARSE_DATETIME_FORMATS, tz: ZoneInfo | None = None
) -> datetime:
    """Interpreta um texto como data e hora usando a primeira máscara compatível.

    Args:
        value: Texto (espaços nas pontas são ignorados).
        formats: Máscaras `strptime`, na ordem de tentativa.
        tz: Fuso do resultado; `None` usa `DEFAULT_TZ`.

    Returns:
        `datetime` com `tzinfo=tz or DEFAULT_TZ` (valores com fuso são convertidos).

    Raises:
        DataFormatError: Se nenhuma máscara casar, com o valor e as máscaras em `details`.
    """
    parsed = _parse(value, formats, "Data e hora inválidas")
    zone = tz or DEFAULT_TZ
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def format_date(value: date | datetime, fmt: str = DEFAULT_DATE_FORMAT) -> str:
    """Formata data ou data e hora com `strftime`.

    Para nomes de meses e dias em português, use `format_date_long`, `month_name` e
    `weekday_name` (`%B` e `%A` dependem do `locale`).

    Args:
        value: Data ou data e hora.
        fmt: Máscara `strftime`.

    Returns:
        Texto formatado (padrão `dd/mm/aaaa`).
    """
    return value.strftime(fmt)


def format_date_long(value: date) -> str:
    """Formata a data por extenso em português.

    Args:
        value: Data.

    Returns:
        Texto como `"31 de outubro de 2026"`.
    """
    return f"{value.day} de {MONTHS_PT[value.month - 1]} de {value.year}"


def month_name(month: int) -> str:
    """Nome do mês em português, em minúsculas.

    Args:
        month: Mês de 1 a 12.

    Returns:
        Nome do mês (ex.: `"março"`).

    Raises:
        ValueError: Se o mês estiver fora de 1 a 12.
    """
    if not 1 <= month <= len(MONTHS_PT):
        raise ValueError(f"Mês inválido: {month}")
    return MONTHS_PT[month - 1]


def weekday_name(value: date) -> str:
    """Nome do dia da semana em português.

    Args:
        value: Data.

    Returns:
        Nome do dia (ex.: `"sábado"`).
    """
    return WEEKDAYS_PT[value.weekday()]


def add_days(value: date, days: int) -> date:
    """Soma dias corridos (aceita valores negativos).

    Args:
        value: Data de referência.
        days: Quantidade de dias.

    Returns:
        Nova data.
    """
    return value + timedelta(days=days)


def add_months(value: date, months: int) -> date:
    """Soma meses, ajustando para o último dia do mês quando o dia não existe.

    Args:
        value: Data de referência.
        months: Quantidade de meses (aceita valores negativos).

    Returns:
        Nova data (ex.: 31/01 + 1 mês = 28/02 ou 29/02).
    """
    total = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def first_day_of_month(value: date) -> date:
    """Primeiro dia do mês da data.

    Args:
        value: Data de referência.

    Returns:
        Data com dia 1.
    """
    return value.replace(day=1)


def last_day_of_month(value: date) -> date:
    """Último dia do mês da data.

    Args:
        value: Data de referência.

    Returns:
        Data com o último dia do mês.
    """
    return value.replace(day=monthrange(value.year, value.month)[1])


def date_range(start: date, end: date, *, step_days: int = 1) -> Iterator[date]:
    """Itera as datas de `start` a `end`, inclusive.

    Com `step_days` negativo, itera em ordem decrescente (de `start` até `end`, com
    `start >= end`).

    Args:
        start: Data inicial.
        end: Data final (incluída quando alcançada).
        step_days: Passo em dias, diferente de zero.

    Returns:
        Iterador de datas.

    Raises:
        ValueError: Se `step_days` for zero.
    """
    if step_days == 0:
        raise ValueError("step_days não pode ser zero")
    return _iter_dates(start, end, timedelta(days=step_days))


def is_business_day(value: date, calendar: HolidayCalendar | None = None) -> bool:
    """Indica se a data é dia útil (segunda a sexta e não feriado).

    Args:
        value: Data.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        `True` para dias úteis.
    """
    return (calendar or _DEFAULT_CALENDAR).is_business_day(value)


def add_business_days(value: date, days: int, calendar: HolidayCalendar | None = None) -> date:
    """Soma dias úteis (aceita valores negativos).

    Args:
        value: Data de referência.
        days: Dias úteis a somar; `0` retorna a própria data, se útil, ou o próximo dia útil.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        Nova data.

    Example:
        >>> add_business_days(date(2026, 4, 2), 1)  # 03/04/2026 é Sexta-feira Santa
        datetime.date(2026, 4, 6)
    """
    selected = calendar or _DEFAULT_CALENDAR
    current = _as_date(value)
    if days == 0:
        return next_business_day(current, include_today=True, calendar=selected)
    step = _ONE_DAY if days > 0 else -_ONE_DAY
    remaining = abs(days)
    while remaining:
        current += step
        if selected.is_business_day(current):
            remaining -= 1
    return current


def next_business_day(
    value: date, *, include_today: bool = False, calendar: HolidayCalendar | None = None
) -> date:
    """Próximo dia útil.

    Args:
        value: Data de referência.
        include_today: Retorna a própria data quando ela for útil.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        O próximo dia útil.
    """
    return _seek_business_day(_as_date(value), _ONE_DAY, include_today, calendar)


def previous_business_day(
    value: date, *, include_today: bool = False, calendar: HolidayCalendar | None = None
) -> date:
    """Dia útil anterior.

    Args:
        value: Data de referência.
        include_today: Retorna a própria data quando ela for útil.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        O dia útil anterior.
    """
    return _seek_business_day(_as_date(value), -_ONE_DAY, include_today, calendar)


def business_days_between(
    start: date, end: date, *, inclusive: bool = True, calendar: HolidayCalendar | None = None
) -> int:
    """Conta os dias úteis entre duas datas.

    Com `inclusive=True`, conta o intervalo fechado `[start, end]`; com `inclusive=False`,
    exclui `start`, de forma que `business_days_between(d, add_business_days(d, n),
    inclusive=False) == n`. Com `start > end`, retorna o valor negativo da contagem do
    intervalo invertido.

    Args:
        start: Data inicial.
        end: Data final.
        inclusive: Inclui a data inicial na contagem.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        Quantidade de dias úteis (negativa quando `start > end`).
    """
    first, last = _as_date(start), _as_date(end)
    if first > last:
        return -business_days_between(last, first, inclusive=inclusive, calendar=calendar)
    selected = calendar or _DEFAULT_CALENDAR
    begin = first if inclusive else first + _ONE_DAY
    return sum(1 for day in _iter_dates(begin, last, _ONE_DAY) if selected.is_business_day(day))


def nth_business_day(
    year: int, month: int, n: int, calendar: HolidayCalendar | None = None
) -> date:
    """N-ésimo dia útil do mês.

    Args:
        year: Ano.
        month: Mês de 1 a 12.
        n: Posição do dia útil, a partir de 1.
        calendar: Calendário de feriados; `None` usa os feriados nacionais do Brasil.

    Returns:
        A data do n-ésimo dia útil.

    Raises:
        ValueError: Se `n` for menor que 1 ou maior que a quantidade de dias úteis do mês.

    Example:
        >>> nth_business_day(2026, 11, 1)  # 02/11 é Finados
        datetime.date(2026, 11, 3)
    """
    if n < 1:
        raise ValueError(f"n deve ser maior ou igual a 1 (recebido: {n})")
    start = date(year, month, 1)
    selected = calendar or _DEFAULT_CALENDAR
    business_days = [
        day
        for day in _iter_dates(start, last_day_of_month(start), _ONE_DAY)
        if selected.is_business_day(day)
    ]
    if n > len(business_days):
        raise ValueError(
            f"{month:02d}/{year} possui {len(business_days)} dias úteis (solicitado: {n})"
        )
    return business_days[n - 1]


@functools.lru_cache(maxsize=256)
def _library_holidays(
    country: str, subdiv: str | None, include_optional: bool, year: int
) -> Mapping[date, str]:
    """Feriados da biblioteca `holidays` para o ano, em cache e somente leitura.

    Os nomes usam o idioma padrão do país, para não depender das variáveis de `locale`.
    """
    import holidays

    categories = (_PUBLIC_CATEGORY, _OPTIONAL_CATEGORY) if include_optional else (_PUBLIC_CATEGORY,)
    try:
        language = holidays.country_holidays(country, subdiv=subdiv).default_language
        result = holidays.country_holidays(
            country, subdiv=subdiv, years=year, categories=categories, language=language
        )
    except (NotImplementedError, ValueError) as exc:  # país/UF: NotImplementedError; categoria
        raise ConfigurationError(
            "Calendário de feriados não suportado pela biblioteca holidays",
            details={
                "pais": country,
                "subdivisao": subdiv,
                "categorias": ", ".join(categories),
                "erro": str(exc),
            },
        ) from exc
    return MappingProxyType(dict(sorted(result.items())))


def _as_date(value: date) -> date:
    """Reduz `datetime` à data (com fuso, convertido para `DEFAULT_TZ`)."""
    if isinstance(value, datetime):
        return value.astimezone(DEFAULT_TZ).date() if value.tzinfo is not None else value.date()
    return value


def _parse(value: str, formats: Sequence[str], message: str) -> datetime:
    text = value.strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise DataFormatError(message, details={"valor": value, "formatos": ", ".join(formats)})


def _iter_dates(start: date, end: date, step: timedelta) -> Iterator[date]:
    current = start
    forward = step > timedelta(0)
    while (current <= end) if forward else (current >= end):
        yield current
        current += step


def _seek_business_day(
    value: date, step: timedelta, include_today: bool, calendar: HolidayCalendar | None
) -> date:
    selected = calendar or _DEFAULT_CALENDAR
    current = value if include_today else value + step
    while not selected.is_business_day(current):
        current += step
    return current
