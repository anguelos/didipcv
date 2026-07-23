import re
import anyascii

# TODO: (anguelos) add more month names.
month2num = {
    "janner": 1,
    "januar": 1,
    "leden": 1,  # Czeck?
    "gennaio": 1,  # Italian?
    "i": 1,
    "feb.": 2,
    "februar": 2,
    "unor": 2,  # Czeck?
    "ii": 2,
    "marz": 3,
    "marzo": 3,  # Italian?
    "brezen": 3,  # Czeck?
    "iii": 3,
    "april": 4,
    "iv": 4,
    "duben": 4,  # Czeck? https://en.wikipedia.org/wiki/Slavic_calendar
    "mai": 5,
    "maggio": 5,  # Italian?
    "v": 5,
    "kveten": 5,  # Hungarian?
    "juni": 6,
    "vi": 6,
    "juli": 7,
    "luglio": 7,  # Italian?
    "cervenec": 7,  # Czeck?
    "vii": 7,
    "cerven": 6,  # Czeck?
    "august": 8,
    "srpen": 8,  # Czeck?
    "viii": 8,
    "agosto": 8,  # Italian?
    "september": 9,
    "settembre": 9,  # Italian?
    "septiembre": 9,  # Spanish?
    "zari": 9,  # Czeck?
    "ix": 9,
    "oktober": 10,
    "ottobre": 10,  # Italian?
    "okt": 10,
    "x": 10,
    "rijen": 10,  # Czeck?
    "november": 11,
    "xi": 11,
    "listopad": 11,  # Czeck?
    "dezember": 12,
    "dicembre": 12,  # Italian?
    "xii": 12,
    "prosinec": 12,  # Czeck?
}


def remove_ambiguous_9(*date_tuple):
    assert len(date_tuple) == 3
    if date_tuple[0] == 9999:
        date_tuple = (0, date_tuple[1], date_tuple[2])
    if date_tuple[1] == 99:
        date_tuple = (date_tuple[0], 0, date_tuple[2])
    if date_tuple[2] == 99:
        date_tuple = (date_tuple[0], date_tuple[1], 0)
    return date_tuple


def is_plausible_date(date_tuple):
    # TODO add 30-31 day checks.
    # TODO add leap year checks.
    # TODO add calendar reformation checks.
    if date_tuple[0] < 0 or date_tuple[0] > 2100:
        return False
    if date_tuple[1] < 0 or date_tuple[1] > 12:
        return False
    if date_tuple[2] < 0 or date_tuple[2] > 31:
        return False
    return True

# Tuple[int, int , int]
# Tuple[Tuple[int, int , int], Tuple[int, int , int]]
# Union[Tuple[Tuple[int, int , int], Tuple[int, int , int]], Tuple[int, int , int]]


def infer_date(date_str: str, fail_quietly: bool = False):
    """Tries to infer a date tuple from a string.

    Args:
        date_str (str): A unicode string that someone somewhen intended to be a date.
        fail_quietly (bool, optional): Whether uparseable dates should be considered 
        undefined or raise a ValueError. Defaults to False.

    Raises:
        ValueError: When parsing fails and fail_quietly is False.

    Returns:
        tuple(int, int, int): A tuple containing the year as the first element, the month 
        as the second and the day as the third. Unknown entries are replaced with 0.
    """
    date_str = anyascii.anyascii(date_str).lower(
    )  # TODO (anguelos) can we remove anyascii dependency?
    date_str = date_str.replace("wohl", "")  # We assume all is aproximate.
    date_str = " ".join(date_str.split())  # Remove extra spaces.
    # YYYMMDD we cant really know what is what but on 1000 Charters, that makes sence eg:25c52625b0576a7eec1a573cda314327/cei.xml
    if re.match("^[0-9]{7}$", date_str):
        date = remove_ambiguous_9(int(date_str[:3]), int(
            date_str[3:5]), int(date_str[5:7]))
        if is_plausible_date(date):
            return date

    if re.match("^1[0-9]{7}$", date_str):  # 1YYYMMDD assuming 1000-1999
        date = remove_ambiguous_9(int(date_str[:4]), int(
            date_str[4:6]), int(date_str[6:8]))
        if is_plausible_date(date):
            return date

    if re.match("^[0-9]{4}1[0-9]{3}$", date_str):  # DDMMYYYY
        date = remove_ambiguous_9(int(date_str[4:]), int(
            date_str[4:6]), int(date_str[6:8]))
        if is_plausible_date(date):
            return date

    if re.match("^[0-9\-,\.\s]{10}$", date_str):
        date = re.split("\-|\.|,|\s", date_str)
        if len(date) == 3 and len(date[2]) in (3, 4):
            date = date[::-1]
        if len(date[0]) == 4 and date[0][0] == "1":
            date = remove_ambiguous_9(int(date[0]), int(date[1]), int(date[2]))
            if is_plausible_date(date):
                return date
        else:
            return f"Unparsed_V1: '{date_str}', {repr(date)}"

    if re.match("^[0-9]+\.[0-9]+\.[0-9]+$", date_str):
        date = re.split("\.", date_str)
        if len(date) == 3 and len(date[2]) in (3, 4):
            date = date[::-1]
        if len(date[0]) in (3, 4) and len(date[1]) in (1, 2) and len(date[2]) in (1, 2):
            date = remove_ambiguous_9(int(date[0]), int(date[1]), int(date[2]))
            if is_plausible_date(date):
                return date
        else:
            return f"Unparsed_V2: '{date_str}', {repr(date)}"

    # The year is unknow, the date is broken nomater what.
    if re.match("^[0-9]*9{4}[0-9]*$", date_str):
        # TODO: (do we really care about months or days without years?)
        date = remove_ambiguous_9(0, 0, 0)
        if is_plausible_date(date):
            return date

    # Czeck dates.
    if re.match("^[0-9][0-9]?\.\s+[a-z]+\.?\s+[0-9]{3}[0-9]?$", date_str):
        date_list = date_str.split()
        date_list[1] = month2num[date_list[1]]
        date_list[0], date_list[2] = int(date_list[2]), int(
            date_list[0].replace(".", ""))
        date = remove_ambiguous_9(*tuple(date_list))
        if is_plausible_date(date):
            return date

    # EG '1288 dezember 22.'
    if re.match("^[0-9]{3}[0-9]?\s+[a-z]+\.?\s+[0-9][0-9]?\.?$", date_str):
        date_list = date_str.split()
        date_list[1] = month2num[date_list[1]]
        date_list[0], date_list[2] = int(date_list[0]), int(
            date_list[2].replace(".", ""))
        date = remove_ambiguous_9(*tuple(date_list))
        if is_plausible_date(date):
            return date

    if re.match("^[0-9]{4}$", date_str):  # Only year.
        date = remove_ambiguous_9(int(date_str), 0, 0)
        if is_plausible_date(date):
            return date

    else:
        if fail_quietly:
            return (0, 0, 0)
        else:
            raise ValueError(f"Unparsable date: '{date_str}'")
