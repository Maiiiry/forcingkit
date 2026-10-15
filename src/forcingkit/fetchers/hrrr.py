import s3fs
import logging


logger = logging.getLogger(__name__)

# NOAA makes HRRR freely available on AWS S3 without authentication.
# The same bucket (noaa-hrrr-bdp-pds) holds both the rolling operational
# window and the full historical archive back to 2014-07-30.
fs = s3fs.S3FileSystem(anon=True)


# First date for which HRRR data exists in the noaa-hrrr-bdp-pds archive.
# GRIB2 .idx variable patterns for 10-meter wind components.
# Each HRRR .grib2 on S3 has a sidecar .idx listing byte offsets per message.
def _parse_idx(
    idx_text: str, patterns: tuple[str, ...]
) -> list[tuple[int, int | None]]:
    """
    Parse a GRIB2 .idx sidecar file and return byte ranges for matching messages.

    Each .idx line has the format:
        message_number:byte_offset:date:variable:level:forecast_type:

    Returns a list of (start_byte, end_byte) tuples. end_byte is None for the
    last message in the file (meaning read-to-EOF).
    """
    lines = [line.strip() for line in idx_text.strip().splitlines() if line.strip()]

    # Build list of (message_number, byte_offset, raw_line)
    entries = []
    for line in lines:
        parts = line.split(":")
        if len(parts) < 3:
            continue
        try:
            offset = int(parts[1])
        except ValueError:
            continue
        entries.append((offset, line))

    # Find matching messages and compute byte ranges
    ranges = []
    for i, (offset, line) in enumerate(entries):
        if any(pattern in line for pattern in patterns):
            # End byte is the start of the next message, or None for EOF
            end = entries[i + 1][0] if i + 1 < len(entries) else None
            ranges.append((offset, end))

    return ranges


def _fetch_s3_byte_ranges(
    s3_path: str, ranges: list[tuple[int, int | None]], output_path: str
) -> None:
    """
    Download specific byte ranges from an S3 object and concatenate into a
    single valid GRIB2 file. Each GRIB2 message is self-contained, so
    concatenating selected messages produces a valid file.

    Each range is one ranged GET of exactly its bytes. A buffered file
    (``fs.open`` then ``seek`` and ``read``) fetches whole blocks with
    read-ahead, which for 1 to 2 MB messages downloaded about 38 times the
    bytes needed (4.0 GB for 106 MB of messages in one HRRR cycle).
    """
    with open(output_path, "wb") as f:
        for start, end in ranges:
            # end=None reads to the end of the object (the file's last message)
            f.write(fs.cat_file(s3_path, start=start, end=end))
