"""Utility functions and page order specifications for RRC document processing."""

import os

FILE_TYPES_ORDER = [
    "Front_Page_1",
    "Receipts_Other_Sources_1-A",
    "Deliveries_1-B",
    "Receipts_from_Leases_2",
    "Stock_On_Hand_3",
]


def parse_record_filename_order(filename: str):
    """Parses a record filename of the format:

    <ORIGINAL_FILENAME>_preprocessed_<FILE_TYPE>_<OCCURENCE>.pdf

    Returns tuple: (original_filename, file_type_rank, occurrence, filename)
    """
    if not filename or "_preprocessed_" not in filename:
        return (filename or "", len(FILE_TYPES_ORDER) + 1, 0, filename or "")

    original_filename, rest = filename.split("_preprocessed_", 1)
    rest_no_ext = os.path.splitext(rest)[0]

    if "_" in rest_no_ext:
        file_type, occ_str = rest_no_ext.rsplit("_", 1)
        try:
            occ_num = int(occ_str)
        except ValueError:
            occ_num = 999
    else:
        file_type = rest_no_ext
        occ_num = 0

    if file_type in FILE_TYPES_ORDER:
        rank = FILE_TYPES_ORDER.index(file_type)
    else:
        rank = len(FILE_TYPES_ORDER)

    return (original_filename, rank, occ_num, filename)


def reconstruct_records_by_original_filename(records: list) -> list:
    """Sorts/reconstructs records so that all records for an ORIGINAL_FILENAME

    are grouped sequentially, ordered by page type and occurrence.
    """
    if not records:
        return records

    decorated = []
    for idx, rec in enumerate(records):
        fn = rec.get("filename", "") or ""
        orig_fn, rank, occ, _ = parse_record_filename_order(fn)
        decorated.append((orig_fn, rank, occ, idx, rec))

    decorated.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    return [item[4] for item in decorated]
