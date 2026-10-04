"""Exact managed names and database-fenced publication, without path traversal."""

import os
import re
import uuid

from app.models import FileIntent
from app.privacy import guard_operation

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NAME = re.compile(rf"^(?:{UUID}\.pdf|{UUID}\.{UUID}\.part)$")


class ManagedFiles:
    def __init__(self, directory):
        self.directory = directory
        self.fd = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    @staticmethod
    def validate(name):
        if not NAME.fullmatch(name):
            raise ValueError("unmanaged_path")
        return name

    def open_writer(self, name):
        return os.fdopen(
            os.open(
                self.validate(name),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            ),
            "wb",
        )

    def open_reader(self, name):
        return os.fdopen(
            os.open(self.validate(name), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd), "rb"
        )

    def replace(self, source, target):
        self.validate(source)
        self.validate(target)
        # A symlink at either exact intent name is an anomaly, never followed.
        for name in (source, target):
            if self.exists(name):
                info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                import stat

                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("managed_path_anomaly")
        os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def exists(self, name):
        try:
            os.stat(self.validate(name), dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def unlink(self, name):
        name = self.validate(name)
        if self.exists(name):
            import stat

            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("managed_path_anomaly")
            os.unlink(name, dir_fd=self.fd)


def register_writer(sessions, operation_id, lease=None):
    part, final = f"{operation_id}.{uuid.uuid4()}.part", f"{operation_id}.pdf"
    with sessions.begin() as session:
        op = guard_operation(session, operation_id, lease)
        for name in (part, final):
            if not session.get(FileIntent, name):
                session.add(FileIntent(path=name, operation_id=op.id, owner_id=op.owner_id))
    return part, final


def guarded_open(sessions, files, operation_id, name, lease=None):
    with sessions.begin() as session:
        guard_operation(session, operation_id, lease)
        intent = session.get(FileIntent, name)
        if not intent or intent.operation_id != operation_id:
            raise ValueError("unregistered_path")
        # The actual open is inside the owner lock: confirmation cannot fence
        # and unlink between checking permission and creating the pathname.
        return files.open_writer(name)
