__version__ = (0, 1, 4)

import contextlib
import dataclasses
import enum
import functools
import http
import netrc
import os
import pathlib
import shutil
import tempfile
import typing
import urllib.parse

import click
import httpx
import magic
import pydicom
import pydicom.tag


class ROIType(enum.StrEnum):
    """OHIF ROI supported DICOM types."""

    _header_: str | tuple[int, int]
    _modal_: str | typing.Sequence[str]

    @property
    def header(self):
        """DICOM header name or tag."""

        return self._header_

    @property
    def modal(self):
        """
        Modality or sequence of modalities
        compatible with this ROI type.
        """

        return self._modal_

    def __new__(cls, value, modal="", header="SOPClassUID"):
        obj = str.__new__(cls, value)
        obj._value_  = value
        obj._header_ = header
        obj._modal_  = modal

        return obj

    Segmentation_Storage = "1.2.840.10008.5.1.4.1.1.66.4", "SEG"
    SEG = Segmentation_Storage


@dataclasses.dataclass
class XNATExperiment:
    date:                 str
    dcmPatientBirthDate:  str
    dcmPatientId:         str
    dcmPatientName:       str
    ID:                   str
    id:                   str
    label:                str
    prearchivePath:       str
    project:              str
    scanner_manufacturer: str
    scanner_model:        str
    session_type:         str
    subject_ID:           str
    UID:                  str


@dataclasses.dataclass
class XNATScan:
    frames:                 int
    ID:                     str
    image_session_ID:       str
    modality:               str
    parameters_fov_x:       int
    parameters_fov_y:       int
    parameters_orientation: str
    parameters_voxelRes_x:  float
    parameters_voxelRes_y:  float
    parameters_voxelRes_z:  float
    project:                str
    quality:                str
    series_description:     str
    type:                   str
    UID:                    str
    xnat_imagescandata_id:  int
    xnat_imageScanData_id:  int


@dataclasses.dataclass
class XNATSubject:
    ID:      str
    label:   str
    project: str


@dataclasses.dataclass
class OHIFNamespace:
    host:     str
    files:    typing.Sequence[pathlib.Path]
    username: typing.Optional[str]
    password: typing.Optional[str]
    port:     typing.Optional[int]
    verbose:  int


# Used to pass the top-level namespace context
# to commands lower than the entry point.
pass_clinamespace = click.make_pass_decorator(OHIFNamespace)


def auth_netrc(namespace: OHIFNamespace) -> tuple[str, str]:
    """
    Attempt to get credentials from `~/.netrc`
    file.
    """

    default = ("", "", "")
    host  = namespace.host
    colon = host.rfind(":")
    if ":" in host and "//" not in host[colon:]:
        host = host[:colon]

    # Single out the hostname.
    host_parsed = urllib.parse.urlparse(host)
    host = host_parsed.netloc or host_parsed.path

    try:
        return (netrc.netrc().authenticators(host) or default)[::2]
    except FileNotFoundError:
        return ("", "")


def dicom_find_files(
        *paths: pathlib.Path,
        strict: bool | None = None) -> typing.Sequence[pathlib.Path]:
    """
    Find all files in given directories. Validate
    that files are DICOM image files.
    """

    files = []

    for path in paths:
        if not path.is_dir() and dicom_isdicom_file(path):
            files.append(path)
            continue
        elif not path.is_dir() and strict in (True, None):
            raise ValueError(f"{path!r} is not a valid DICOM image file.")

        for dpath, _, dpath_files in os.walk(path):
            dpath = pathlib.Path(dpath) #type: ignore[assignment]
            if not dpath_files:
                continue
            files.extend(dicom_find_files(
                *map(dpath.joinpath, dpath_files), #type: ignore[attr-defined]
                strict=False))

    # Validate that the files found all have the
    # same StudyInstanceUID value.
    study_instance_uids = {dicom_get(f, "StudyInstanceUID") for f in files}
    if len(study_instance_uids) > 1:
        ohif_error("Files found have more than one StudyInstanceUID")
        ohif_panic(f"Found in files {study_instance_uids}")

    return tuple(files)


T = typing.TypeVar("T")
@typing.overload
def dicom_get(
    path: pathlib.Path,
    key: tuple[int, int]) -> pydicom.DataElement:
    pass
@typing.overload
def dicom_get(
    path: pathlib.Path, key: tuple[int, int],
    default: T) -> pydicom.DataElement | T:
    pass
@typing.overload
def dicom_get(path: pathlib.Path, key: str) -> typing.Any:
    pass
@typing.overload
def dicom_get( #type: ignore
    path: pathlib.Path,
    key: str, default: T) -> typing.Any | T:
    pass
def dicom_get(path, key, default=...):
    """Retrieve header element from DICOM image."""

    if default == Ellipsis:
        return pydicom.dcmread(path).get(key)
    return pydicom.dcmread(path).get(key, default)


def dicom_isdicom_file(path: pathlib.Path) -> bool:
    """
    Given path is a file, exists, and is a DICOM
    file.
    """

    if not path.exists():
        return False
    if path.is_dir():
        return False

    return magic.from_file(str(path)) == "DICOM medical imaging data"


def dicom_isroi_type(path: pathlib.Path, roi_type: str | ROIType) -> bool:
    """
    Path is to valid DICOM file and DICOM headers
    indicate the file is a valid ROI file.
    """

    if not dicom_isdicom_file(path):
        raise ValueError(f"{path!r} is not a valid DICOM image file.")

    roi_type = ROIType[roi_type] if isinstance(roi_type, str) else roi_type
    return (
        dicom_isdicom_file(path) and
        dicom_get(path, roi_type.header) == roi_type.value and #type: ignore[union-attr]
        dicom_get(path, "Modality") in roi_type.modal) #type: ignore[union-attr]


def dicom_set(
        path: pathlib.Path,
        key: str | tuple[int, int],
        VR: str,
        value: typing.Any) -> None:
    """Set the field value of a DICOM header."""

    if not dicom_isdicom_file(path):
        raise ValueError(f"{path!r} is not a valid DICOM image file.")

    dicom = pydicom.dcmread(path)
    dicom[key] = pydicom.DataElement(key, VR, value)
    pydicom.dcmwrite(path, dicom)


def ohif_error(*values: str, sep: str | None = None) -> None:
    """Write a message to `stderr`."""

    message = f"{click.style('error', fg='red')}: " + (sep or " ").join(values)
    click.echo(message, err=True)


def ohif_info(
        namespace: OHIFNamespace,
        *values: str,
        sep: str | None = None,
        level: int | None = None) -> None:
    """
    Write a message to `stdout`. Outputs only if
    `namespace.verbose` is greater than or equal
    to `level`.
    """

    if namespace.verbose < (level or 0):
        return None
    message = f"{click.style('info', fg='green')}: " + (sep or " ").join(values)
    click.echo(message)


def ohif_panic(
        *values: str,
        sep: str | None = None,
        code: int | None = None) -> typing.NoReturn:
    """Write message to `stderr` and quit."""

    ohif_error(*values, sep=sep)
    quit(code or 1)


def ohif_strict_quitter(
        code: int | None = None,
        *,
        strict: str | None = None) -> None | typing.NoReturn:
    """
    Exits the program using the built-in `quit`
    call if called with `strict` as `True`.
    """

    strict = "quitter" if strict is None else strict
    if strict == "quitter":
        quit(code)
    if strict == "raise":
        raise
    return None


def rest_auth(namespace: OHIFNamespace) -> httpx.Auth:
    """Parse credentials for the remote XNAT."""

    username, password = auth_netrc(namespace)
    # Allow for user defined override at CLI
    # execution.
    username = namespace.username or username
    password = namespace.password or password
    return httpx.BasicAuth(username, password)


@contextlib.contextmanager
def rest_client(
        namespace: OHIFNamespace,
        *,
        strict: typing.Optional[str] = None,
        verify: typing.Optional[bool] = None):
    """
    Create a REST client to make calls against the
    remote XNAT. HTTP exceptions raised in this
    context will panic, writing a message to
    stderr.
    """

    client = httpx.Client(
        auth=rest_auth(namespace),
        base_url=rest_host(namespace),
        verify=verify if verify is not None else True)

    try:
        yield client
    except httpx.HTTPStatusError as error:
        method, path, code, phrase = rest_extract_error(error)
        ohif_error(f"({method}) {path} failed: <{code} {phrase!r}>")
        if error.response.text:
            click.echo(error.response.text, err=True)
        ohif_strict_quitter(1, strict=strict)
    except httpx.RequestError as error:
        method, path, *_ = rest_extract_error(error)
        ohif_error(f"({method}) {path} failed: {error}")
        ohif_strict_quitter(1, strict=strict)



def rest_extract_error(err: httpx.HTTPError) -> tuple[str, str, int, str]:
    """
    Extract error information from an
    `httpx.HTTPError`.
    """

    args = [
        err.request.method,
        err.request.url.path,
        500,
        "Internal Server Error"]

    if isinstance(err, httpx.HTTPStatusError):
        status_code = err.response.status_code
        args[2] = status_code
        args[3] = http.HTTPStatus(status_code).phrase

    return tuple(args) #type: ignore[return-value]


def rest_host(namespace: OHIFNamespace) -> str:
    """Parse host base URL."""

    # Weed out the port number.
    host  = namespace.host
    colon = host.rfind(":")
    if ":" in host and "//" not in host[colon:]:
        host, port = host[:colon], int(host[colon+1:])
    else:
        host, port = host, -1

    port = namespace.port or port

    # Rotating through the given host argument to
    # ensure the anatomy of the URL is correct.
    parsed = urllib.parse.urlparse(host)
    parsed = urllib.parse.ParseResult(
        parsed.scheme or "https",
        parsed.netloc or parsed.path,
        "",
        parsed.params,
        parsed.query,
        parsed.fragment)

    uri = parsed.geturl()

    if port > 0:
        uri += f":{port}"

    return  uri


class REST:
    """
    Common RESTful operations against an XNAT.
    """

    @classmethod
    def _object_getter(
        cls,
        namespace: OHIFNamespace,
        uri: str) -> dict[str, typing.Any]:
        """
        Get the raw contents of an object from an
        XNAT.
        """

        with rest_client(namespace, strict="raise") as rest:
            r = rest.get(uri, params=dict(format="json"))
            r.raise_for_status()

            ohif_info(
                namespace,
                f"(GET) {r.url.path} ({r.status_code})",
                level=4)
            return r.json()["items"][0]["data_fields"]

    @classmethod
    def _object_putter(
            cls,
            namespace: OHIFNamespace,
            uri: str,
            **params) -> str:
        """
        Raw PUT request for some XNAT endpoint.
        """

        with rest_client(namespace, strict="raise") as rest:
            r = rest.put(uri, params=params)
            r.raise_for_status()

            ohif_info(
                namespace,
                f"(PUT) {r.url.path} ({r.status_code})",
                level=4)
            return r.text

    @classmethod
    def aquire_scan(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        scan: str,
        xsi_type: str) -> XNATScan:
        """
        Attempt to get an existing scan. If none
        exists on the remote XNAT, create the
        instance and return the created scan.
        """

        common_args = namespace, project, subject, session, scan
        getter = functools.partial(cls.get_scan, *common_args)
        putter = functools.partial(cls.put_scan, *common_args, xsi_type)
        try:
            return getter()
        except httpx.HTTPStatusError:
            putter()
            return getter()

    @classmethod
    def aquire_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str) -> XNATExperiment:
        """
        Attempt to get an existing session. If
        none exists on the remote XNAT, create the
        instance and return the created session.
        """

        common_args = namespace, project
        getter = functools.partial(cls.get_session, *common_args)
        putter = functools.partial(cls.put_session, *common_args)

        try:
            return getter(session)
        except httpx.HTTPStatusError:
            putter(subject, session)
            return getter(session)

    @classmethod
    def aquire_subject(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str):
        """
        Attempt to get an existing subject. If
        none exists on the remote XNAT, create the
        instance and return the created subject.
        """

        common_args = namespace, project, subject
        getter = functools.partial(cls.get_subject, *common_args)
        putter = functools.partial(cls.put_subject, *common_args)

        try:
            return getter()
        except httpx.HTTPStatusError:
            putter()
            return getter()

    @classmethod
    def get_username(cls, namespace: OHIFNamespace) -> str:
        """
        Get the username associated with the REST
        session.
        """

        with rest_client(namespace, strict="quitter") as rest:
            r = rest.get("/xapi/users/username")
            r.raise_for_status()

        return r.text

    @classmethod
    def get_scan(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        scan: str) -> XNATScan:
        """Get scan data from an XNAT."""

        data = cls._object_getter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}"
            f"/scans/{scan}")

        for key in data.copy().keys():
            # Must replace all '/' chars to make
            # data dataclass digestable.
            data[key.replace("/", "_")] = data.pop(key)

        return XNATScan(**data)

    @classmethod
    def get_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            session: str) -> XNATExperiment:
        """Get session data from an XNAT."""

        data = cls._object_getter(
                namespace,
                f"/data/projects/{project}/experiments/{session}")

        data["scanner_model"]        = data.pop("scanner/model")
        data["scanner_manufacturer"] = data.pop("scanner/manufacturer")
        return XNATExperiment(**data)

    @classmethod
    def get_subject(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str):
        """Get subject data from an XNAT."""

        data = cls._object_getter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}")
        return XNATSubject(**data)

    @classmethod
    def put_scan(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str,
            scan: str,
            xsi_type: str):
        """Create a new scan on a remote XNAT."""

        return cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}"
            f"/scans/{scan}",
            xsiType=xsi_type)

    @classmethod
    def put_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str) -> str:
        """
        Create a new session on a remote XNAT.
        """

        return cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}"
        )

    @classmethod
    def put_subject(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str) -> str:
        """
        Create a new subject on a remote XNAT.
        """

        return cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}")


class RESTOHIF:
    """Namespace for OHIF related operations."""

    @classmethod
    def roi_store(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        *,
        label: typing.Optional[str],
        roi_type: str,
        overwrite: bool) -> None:
        """
        Attempt to store segment data, and correlating
        session data, in a remote XNAT.
        """

        xsession = REST.aquire_session(namespace, project, subject, session)
        xsubject = REST.aquire_subject(namespace, project, subject)

        ohif_info(
            namespace,
            f"found {len(namespace.files)} DICOM files.",
            level=1)
        ohif_info(namespace, f"pushing to store {roi_type} data.", level=1)

        for file in namespace.files:
            args = namespace, project, xsubject, xsession, file

            # Push non-ROI data as regular session
            # data.
            if not dicom_isroi_type(file, roi_type):
                cls.roi_store_subject(*args)
                continue

            # Push ROI data as a collection.
            kwds = dict(label=label, roi_type=roi_type, overwrite=overwrite)
            cls.roi_store_segment(*args, **kwds) #type: ignore[arg-type]
        ohif_info(namespace, "done.", level=2)

    @classmethod
    def roi_store_segment(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: XNATSubject,
            session: XNATExperiment,
            file: pathlib.Path,
            *,
            label: typing.Optional[str],
            roi_type: str,
            overwrite: bool) -> None:
        """
        Attempt to send a PUT request to XNAT to
        store an ROI collection.
        """

        if not label:
            label = (
                dicom_get(file, "SeriesDescription")
                .replace(" ", "_")
                .replace(subject.label, session.label)
        )

        uri = (
            f"/xapi/roi/projects/{project}"
            f"/sessions/{session.ID}/collections/{label}")

        headers = dict()
        headers["Content-Type"] = "application/octet-stream"

        params = dict()
        params["overwrite"] = str(overwrite or False).lower()
        params["seriesuid"] = ""
        params["type"]      = roi_type
        params["seriesuid"] = dicom_get(file, "SeriesInstanceUID")

        with contextlib.ExitStack() as es:
            rest  = es.enter_context(rest_client(namespace, strict=False))
            twd = pathlib.Path(es.enter_context(tempfile.TemporaryDirectory()))

            ohif_info(namespace, f"attempting to push {file!s}", level=2)
            # Create a copy of the target file to
            # validate and push to XNAT.
            shutil.copyfile(str(file), str(twd.joinpath("image.dcm")))
            file = twd.joinpath("image.dcm")
            cls.roi_validate_segment(namespace, file)

            # Push validated file to collection. 
            r = rest.put(
                uri,
                data=es.enter_context(file.open("rb")),
                headers=headers,
                params=params)
            r.raise_for_status()
            ohif_info(
                namespace,
                f"(PUT) {r.url.path} ({r.status_code})",
                level=4)

    @classmethod
    def roi_store_subject(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: XNATSubject,
            session: XNATExperiment,
            file: pathlib.Path) -> None:
        """
        Attempt to create a session related to segment
        data from file.
        """

        # SeriesNumber == scan id
        # Attempt to create the scan if it does
        # not already exist.
        scan = REST.aquire_scan(
            namespace,
            project,
            subject.label,
            session.label,
            dicom_get(file, "SeriesNumber"),
            dicom_get(file, "Modality").lower() + ":ScanData")

        uri = (
            f"/data/experiments/{session.ID}/scans/{scan.ID}/resources/"
            f"DICOM/files/{file.name}")

        with contextlib.ExitStack() as es:
            rest = es.enter_context(rest_client(namespace))
            fd   = file.open("rb")

            ohif_info(namespace, f"attempting to upload {file!s}", level=2)
            r = rest.post(uri, params=dict(inbody="true"), data=fd)
            r.raise_for_status()

            ohif_info(
                namespace,
                f"(POST) {r.url.path} ({r.status_code})",
                level=4)

    @classmethod
    def roi_validate_segment(
            cls,
            namespace: OHIFNamespace,
            file: pathlib.Path) -> None:
        """
        Validate a segment file. Ensure data is
        clean and of what the OHIF plugin
        expects.
        """

        # Validate fields are not missing or
        # unset, and if not, set to "Unknown".
        field = dicom_get(file, "SoftwareVersions", "")
        if not field:
            ohif_info(
                namespace,
                f"fixing SoftwareVersions with field {field!r}",
                level=3)
            dicom_set(file, "SoftwareVersions", "LO", "Unknown")

        field = dicom_get(file, "StudyID", None)
        if field in ("", None):
            ohif_info(
                namespace,
                f"fixing StudyID with field {field!r}",
                level=3)
            dicom_set(file, "StudyID", "SH", "Unknown")


@click.group()
@click.pass_context
@click.version_option(".".join(map(str, __version__)))
@click.option("--host", "-H")
@click.option("--username", "-u", default=None)
@click.option("--password", "-p", default=None)
@click.option("--port", "-P", type=int, default=None)
@click.option("--verbose", "-v", count=True)
def ohif(
    ctx: click.Context,
    *,
    host: str,
    username: typing.Optional[str],
    password: typing.Optional[str],
    port: typing.Optional[int],
    verbose: int):
    """Manage OHIF via XNAT."""

    ctx.obj = OHIFNamespace(host, (), username, password, port, verbose)
    # Validate that credentials are valid by first
    # making an attempt to get their username.
    if host:
        REST.get_username(ctx.obj)

    return 0


@ohif.group()
def roi():
    """OHIF ROI Management."""


@roi.command
@pass_clinamespace
@click.argument("project")
@click.argument("subject")
@click.argument("session")
@click.option(
    "--overwrite/--create",
    "-O/",
    "overwrite",
    help="create or overwrite collection")
@click.option("--file", "-f", "files", type=pathlib.Path, multiple=True)
@click.option("--label", "-l", default=None)
@click.option(
    "--type",
    "-t",
    "roi_type",
    type=click.Choice(["AIM", "RTSTRUCT", "SEG"]),
    default="SEG")
def store(
    namespace: OHIFNamespace,
    project: str,
    subject: str,
    session: str,
    *,
    files: typing.Sequence[pathlib.Path],
    label: typing.Optional[str],
    roi_type: str,
    overwrite: bool):
    """Store an ROI collection."""

    if not namespace.host:
        ohif_panic("no hostname was provided")

    files = dicom_find_files(*files)
    if not files:
        ohif_panic("no files were provided")

    namespace.files = files
    RESTOHIF.roi_store(
        namespace,
        project,
        subject,
        session,
        label=label,
        roi_type=roi_type,
        overwrite=overwrite)


def from_command_line():
    return ohif()


if __name__ == "__main__":
    exit(from_command_line())
