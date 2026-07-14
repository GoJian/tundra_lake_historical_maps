import os
import re
import ast
import pandas as pd
from typing import Optional, Dict, Any, List, Tuple, cast


class MetadataCollector:
    """
    Collects map metadata by parsing .map files under a base directory.

    Methods mirror the original notebook functions, wrapped into a class for reuse.
    """

    def __init__(self, base_dir: Optional[str] = None, verbose: bool = True) -> None:
        self.base_dir = base_dir
        self.verbose = verbose

    # -------------------------
    # Core parsing and collection
    # -------------------------
    @staticmethod
    def parse_detailed_map_file(map_file_path: str) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            'map_file': map_file_path,
            'gif_file': None,
            'year': None,
            'scale': None,
            'index': None,
            'datum': None,
            'projection': None,
            'projection_params': {},
            'image_width': None,
            'image_height': None,
            'control_points': [],  # [{'x': int, 'y': int, 'lat': float, 'lon': float}, ...]
            'corners': {},  # {idx: {'x': int, 'y': int, 'lat': float, 'lon': float}}
        }

        with open(map_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        # Datum is a fixed-position record in the OziExplorer .map header: it is
        # always the 5th line (index 4), e.g.
        #   "Pulkovo 1942 (2),WGS 84,   0.0000,   0.0000,WGS 84"  or
        #   "WGS 84,WGS 84,   0.0000,   0.0000,WGS 84".
        # Reading it positionally (first comma field) captures every datum
        # uniformly. The old heuristic only matched lines starting with "WGS",
        # silently dropping the "Pulkovo 1942 (2)" datum used by ~94% of sheets,
        # which then defaulted to WGS84 with NO datum shift (~105 m error).
        info['datum'] = MetadataCollector.parse_datum_record(lines)

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            if line.endswith('.gif'):
                info['gif_file'] = line

            elif "scale:" in line and "index:" in line and "year:" in line:
                match = re.search(r'scale:(\S+)\s+index:(\S+)\s+year:(\d{4})', line)
                if match:
                    info['scale'], info['index'], info['year'] = match.groups()

            elif line.startswith('Map Projection'):
                parts = line.split(',')
                if len(parts) > 1:
                    info['projection'] = parts[1].strip()

            elif line.startswith('Projection Setup'):
                parts = line.split(',')
                keys = [
                    'latitude_of_origin',
                    'central_meridian',
                    'scale_factor',
                    'false_easting',
                    'false_northing',
                ]
                try:
                    values = list(map(float, parts[1 : 1 + len(keys)]))
                    info['projection_params'] = dict(zip(keys, values))
                except Exception:
                    # Leave empty if parsing fails
                    info['projection_params'] = {}

            elif line.startswith('IWH'):
                # e.g., IWH,px,2510,3221
                try:
                    _, _, width, height = line.split(',')
                    info['image_width'] = int(width)
                    info['image_height'] = int(height)
                except Exception:
                    pass

            elif line.startswith('Point') and ',xy,' in line:
                # e.g., Point,xy,152,307,deg,min,...
                parts = line.split(',')
                try:
                    x = int(parts[2])
                    y = int(parts[3])
                    lat_deg = int(parts[6])
                    lat_min = float(parts[7])
                    lon_deg = int(parts[9])
                    lon_min = float(parts[10])
                    lat = lat_deg + lat_min / 60.0
                    lon = lon_deg + lon_min / 60.0
                    info['control_points'].append({'x': x, 'y': y, 'lat': lat, 'lon': lon})
                except (ValueError, IndexError):
                    # Skip malformed point rows
                    continue

            elif line.startswith('MMPXY'):
                # e.g., MMPXY,1,0,0
                parts = line.split(',')
                try:
                    idx, x, y = int(parts[1]), int(parts[2]), int(parts[3])
                    info['corners'].setdefault(idx, {})['x'] = x
                    info['corners'][idx]['y'] = y
                except Exception:
                    pass

            elif line.startswith('MMPLL'):
                # e.g., MMPLL,1,73.0,67.0
                parts = line.split(',')
                try:
                    idx, lon, lat = int(parts[1]), float(parts[2]), float(parts[3])
                    info['corners'].setdefault(idx, {})['lat'] = lat
                    info['corners'][idx]['lon'] = lon
                except Exception:
                    pass

        return info

    def collect_all_map_metadata(self, base_dir: Optional[str] = None) -> pd.DataFrame:
        base = base_dir or self.base_dir
        if not base:
            raise ValueError(
                "Base directory must be provided either in constructor or as an argument."
            )

        map_info_list: List[Dict[str, Any]] = []
        for root, _, files in os.walk(base):
            for file in files:
                if file.endswith('.map'):
                    map_file_path = os.path.join(root, file)
                    try:
                        map_info = self.parse_detailed_map_file(map_file_path)
                        map_info['gif_path'] = (
                            os.path.join(root, map_info['gif_file'])
                            if map_info['gif_file']
                            else None
                        )
                        map_info_list.append(map_info)
                    except Exception as e:
                        if self.verbose:
                            print(f"Failed to parse {map_file_path}: {e}")

        df = pd.DataFrame(map_info_list)
        return df

    # -------------------------
    # Utilities
    # -------------------------
    @staticmethod
    def ensure_dict(obj: Any) -> Dict[str, Any]:
        if isinstance(obj, str):
            try:
                return ast.literal_eval(obj)
            except Exception:
                return {}
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def parse_datum_record(lines: List[str]) -> Optional[str]:
        """Extract the map datum name from OziExplorer .map header lines.

        The datum record is the 5th line (index 4) of the file, formatted as
        ``<datum>,<datum>,<shift1>,<shift2>,<ellipsoid>``. Returns the first
        comma-separated field (e.g. ``"Pulkovo 1942 (2)"`` or ``"WGS 84"``), or
        None if the header is too short or the field is empty/placeholder.
        """
        if len(lines) <= 4:
            return None
        datum = lines[4].split(',')[0].strip()
        if not datum or datum.lower() in {'map datum', 'reserved 1', 'reserved 2'}:
            return None
        return datum

    @staticmethod
    def extract_year_from_path(map_file_path: str) -> Optional[int]:
        """Extract year from a path with pattern like (1982) or (1982-1983)."""
        match = re.search(r'\((\d{4})(?:-(\d{4}))?\)', map_file_path)
        if match:
            start_year = int(match.group(1))
            return start_year
        return None

    @staticmethod
    def extract_fields_from_mapfile(
        map_file_path: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Try to extract scale, index, datum from the header lines of the map file."""
        scale = index = datum = None
        try:
            with open(map_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            for line in lines:
                if 'scale:' in line and 'index:' in line:
                    match = re.search(r'scale:(\S+)\s+index:(\S+)', line)
                    if match:
                        scale = match.group(1)
                        index = match.group(2)
                if (line.strip().startswith('WGS')) or ('Pulkovo' in line):
                    datum = line.split(',')[0].strip()
                # Early exit if everything found
                if scale and index and datum:
                    break
        except Exception as e:
            # Non-fatal: just return Nones
            pass
        return scale, index, datum

    def fill_missing_metadata(self, df: pd.DataFrame) -> pd.DataFrame:
        """Process rows with missing year, scale, index, or datum and try to fill them."""
        filled_rows = []
        for _, row in df.iterrows():
            updated = row.copy()
            changed = False

            # Fill year
            year_val = row.get('year')
            if bool(pd.isna(cast(object, year_val)) or year_val in ['', None]):
                year = self.extract_year_from_path(str(row.get('map_file', '')))
                if year is not None:
                    updated['year'] = year
                    changed = True

            # Fill scale, index, datum
            scale_val = row.get('scale')
            index_val = row.get('index')
            datum_val = row.get('datum')
            missing_scale = bool(pd.isna(cast(object, scale_val)) or scale_val in ['', None])
            missing_index = bool(pd.isna(cast(object, index_val)) or index_val in ['', None])
            missing_datum = bool(pd.isna(cast(object, datum_val)) or datum_val in ['', None])

            if missing_scale or missing_index or missing_datum:
                scale, index, datum = self.extract_fields_from_mapfile(str(row.get('map_file', '')))
                if missing_scale and scale:
                    updated['scale'] = scale
                    changed = True
                if missing_index and index:
                    updated['index'] = index
                    changed = True
                if missing_datum and datum:
                    updated['datum'] = datum
                    changed = True

            filled_rows.append(updated if changed else row)

        out = pd.DataFrame(filled_rows)
        return out

    # -------------------------
    # Visualization and persistence
    # -------------------------
    @staticmethod
    def plot_year_distribution(
        df: pd.DataFrame,
        figsize=(12, 6),
        bins: int = 30,
        kde: bool = True,
        title: str = 'Distribution of Map Years',
    ) -> None:
        import matplotlib.pyplot as plt
        import seaborn as sns

        plt.figure(figsize=figsize)
        sns.histplot(data=df, x='year', bins=bins, kde=kde)
        plt.title(title)
        plt.xlabel('Year')
        plt.ylabel('Count')
        plt.grid()
        plt.show()

    def save_csv(self, df: pd.DataFrame, output_csv_path: Optional[str] = None) -> str:
        out_path = output_csv_path
        if out_path is None:
            if not self.base_dir:
                raise ValueError("output_csv_path must be provided if base_dir is not set")
            out_path = os.path.join(self.base_dir, 'map_all_metadata.csv')
        df.to_csv(out_path, index=False)
        if self.verbose:
            print(f"Map metadata saved to {out_path}")
        return out_path
