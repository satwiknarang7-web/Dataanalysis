import pandas as pd

from insight.engine.cleaning import clean
from tests.conftest import frame


def test_numbers_as_people_write_them():
    df, report = clean(frame({"revenue": ["$1,200.50", "(300)", "1.2K", "$3M", "45", "n/a"], "k": list("abcdef")}))
    assert df["revenue"].tolist()[:5] == [1200.5, -300.0, 1200.0, 3_000_000.0, 45.0]
    assert pd.isna(df["revenue"].iloc[5])
    assert report.columns[0].kind == "number"


def test_percent_sign_marks_the_unit():
    df, report = clean(frame({"margin": ["12%", "8.5%", "30%"]}))
    assert df["margin"].tolist() == [12.0, 8.5, 30.0]
    assert report.percent_columns() == {"margin"}


def test_sizes_with_scale_letters_stay_text():
    df, _ = clean(frame({"size": ["5M", "XL", "S", "M", "L", "XS"]}))
    assert df["size"].tolist()[0] == "5M"


def test_leading_zeros_are_identifiers_not_numbers():
    df, _ = clean(frame({"zip": ["00123", "04567", "10001", "02134"]}))
    assert df["zip"].tolist() == ["00123", "04567", "10001", "02134"]


def test_whole_numbers_stay_whole():
    df, _ = clean(frame({"year": ["2021", "2022", "2023"]}))
    assert str(df["year"].dtype) == "Int64"


def test_dates_in_the_forms_people_write_them():
    df, report = clean(frame({"d": ["5 Jan 2024", "05-Jan-24", "31.12.2024", "March 2024", "2024-02-01"]}))
    got = df["d"].dt.strftime("%Y-%m-%d").tolist()
    assert got == ["2024-01-05", "2024-01-05", "2024-12-31", "2024-03-01", "2024-02-01"]
    assert report.columns[0].kind == "date"


def test_slash_dates_read_day_first_when_the_values_say_so():
    df, report = clean(frame({"d": ["25/12/2024", "03/04/2024"]}))
    assert df["d"].dt.strftime("%Y-%m-%d").tolist() == ["2024-12-25", "2024-04-03"]
    assert not report.columns[0].guessed


def test_ambiguous_slash_dates_are_flagged_as_guessed():
    _, report = clean(frame({"d": ["03/04/2024", "05/06/2024"]}))
    assert report.columns[0].guessed


def test_spellings_of_one_category_fold_into_the_most_common():
    df, report = clean(frame({"region": ["North", "North", "north", " NORTH ", "South"]}))
    assert df["region"].tolist() == ["North", "North", "North", "North", "South"]
    assert report.merges[0].into == "North"


def test_yes_no_spellings_become_yes_and_no():
    df, _ = clean(frame({"churned": ["Y", "n", "yes", "No", "TRUE"]}))
    assert df["churned"].tolist() == ["Yes", "No", "Yes", "No", "Yes"]


def test_personal_data_is_redacted():
    df, report = clean(
        frame({"note": ["mail ana@example.com", "call +1 555-123-4567", "fine"], "phone": ["5551234567", "5559876543", None]})
    )
    assert df["note"].tolist() == ["mail [email]", "call [phone]", "fine"]
    assert df["phone"].tolist()[:2] == ["[phone]", "[phone]"]
    assert next(c for c in report.checks if c.key == "pii").count == 4


def test_big_numbers_are_measures_not_phones():
    df, _ = clean(frame({"revenue": ["2500000000", "1200000000", "900000000"]}))
    assert df["revenue"].tolist() == [2_500_000_000, 1_200_000_000, 900_000_000]


def test_headers_empty_columns_and_placeholders():
    raw = pd.DataFrame({"": ["a", "b"], "x": ["1", "2"], "x2": ["N/A", "-"]})
    raw.columns = ["", "x", "x"]
    df, report = clean(raw)
    assert list(df.columns) == ["Column 1", "x"]
    assert next(c for c in report.checks if c.key == "empty_columns").count == 1


def test_redacted_placeholders_are_missing():
    df, _ = clean(frame({"phone_owner": ["[REDACTED_PHONE]", "Ana", "Bo"], "n": ["1", "2", "3"]}))
    assert df["phone_owner"].isna().tolist() == [True, False, False]


def test_european_decimal_commas():
    df, _ = clean(frame({"amount": ["1.234,50", "12,5", "3,75"]}))
    assert df["amount"].tolist() == [1234.5, 12.5, 3.75]
