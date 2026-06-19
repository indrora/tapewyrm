"""QIC-117 drive & protocol layer (DESIGN.md §6A.3, §13.1).

Where semantics begin: the command table, the N+2 argument encoder, the drive
dispatch by kind, status/error decoding, error classification, and the per-drive
DriveProfile loader. Wraps a DeviceLink; the command *content* is verbatim but
the *meaning* (dispatch, follow-ups) lives here.
"""
