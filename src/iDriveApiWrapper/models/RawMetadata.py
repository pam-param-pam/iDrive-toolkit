from dataclasses import dataclass

@dataclass
class RawMetadata:
    camera: str
    camera_owner: str
    iso: str
    shutter: str
    aperture: str
    focal_length: str

@dataclass
class PhotoMetadata:
    width: int
    height: int
