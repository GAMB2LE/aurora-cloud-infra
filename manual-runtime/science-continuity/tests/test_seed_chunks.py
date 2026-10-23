"""Regression coverage for the exact deployed rolling-window seed and appender."""
import ast
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys

import numpy as np
import xarray as xr
import zarr

HERE = Path(__file__).parent


def worker_source(name):
    return (HERE.parent if name == 'aurora-science-continuity.py' else HERE) / name


def seed_code(name):
    tree = ast.parse(worker_source(name).read_text())
    return next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SEED_PRODUCT' for t in n.targets))


BEFORE = seed_code('before.py')
CANDIDATE = seed_code('aurora-science-continuity.py')


def run_seed(code, source, destination, floor, chunk):
    return subprocess.run([sys.executable, '-c', code, str(source), str(destination), str(floor), str(chunk)],
                          capture_output=True, text=True, timeout=40)


def tree_hash(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}


def scientific_fixture(n=1001):
    tick = np.arange(n)
    time = np.datetime64('2026-10-21T10:44:51', 'ns') + tick * np.timedelta64(10, 's')
    one = (tick / 7.0).astype('float32')
    one[::37] = np.nan
    profile = (tick[:, None] / 13 + np.arange(94)[None, :] / 17).astype('float32')
    profile[::41, 10:18] = np.nan
    ds = xr.Dataset({
        'LWP': ('time', one), 'IWV': ('time', one + 2),
        'SURF_T': ('time', one + 273.15),
        'T_PROF': (('time', 'range'), profile),
        'T_PROF_CMP': (('time', 'range'), profile + 1),
    }, coords={'time': time, 'range': np.arange(94, dtype='float64') * 25},
       attrs={'title': 'Exact scientific fixture', 'history': 'preserve this attribute'})
    ds.LWP.attrs['units'] = 'g m-2'
    ds.T_PROF.attrs['units'] = 'K'
    ds.range.attrs['units'] = 'm'
    return ds


def write_source(ds, path, time_chunk):
    ds.chunk({'time': time_chunk, 'range': 48}).to_zarr(path, mode='w', consolidated=True)


def test_only_seed_serialization_changes():
    trees = []
    for name in ('before.py', 'aurora-science-continuity.py'):
        tree = ast.parse(worker_source(name).read_text())
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SEED_PRODUCT' for t in n.targets):
                n.value = ast.Constant(value='SEED_PRODUCT')
        trees.append(ast.dump(tree, include_attributes=False))
    assert trees[0] == trees[1], 'No worker guards, routes, algorithms, locks, phase budgets or WXcam code may change'
    assert 'safe_chunks=True' in CANDIDATE
    assert 'safe_chunks=False' not in CANDIDATE


def test_reproduces_original_297_300_failure(tmp_path):
    source = tmp_path / 'source.zarr'
    ds = scientific_fixture()
    write_source(ds, source, 297)
    result = run_seed(BEFORE, source, tmp_path / 'before.zarr', ds.time.values[31] + np.timedelta64(1, 's'), 300)
    assert result.returncode != 0
    assert 'would overlap multiple Dask chunks' in result.stderr
    assert '297' in result.stderr and '300' in result.stderr


def check_first_trim_preserves_all_values_coords_attrs_and_source(tmp_path, source_chunk, new_chunk):
    ds = scientific_fixture()
    source, destination = tmp_path / 'source.zarr', tmp_path / 'candidate.zarr'
    write_source(ds, source, source_chunk)
    original = tree_hash(source)
    floor = ds.time.values[31] + np.timedelta64(1, 's')
    result = run_seed(CANDIDATE, source, destination, floor, new_chunk)
    assert result.returncode == 0, result.stderr
    with xr.open_zarr(destination, chunks={}) as actual:
        xr.testing.assert_identical(actual.load(), ds.sel(time=slice(floor, None)))
    store = zarr.open_group(destination, mode='r')
    assert store['LWP'].chunks == (new_chunk,)
    assert store['T_PROF'].chunks == (new_chunk, 48)
    assert store['T_PROF'].shape[1] == 94  # The terminal range chunk has 46 values.
    assert tree_hash(source) == original


def test_hatpro_first_trim(tmp_path):
    check_first_trim_preserves_all_values_coords_attrs_and_source(tmp_path, 297, 300)


def test_cl61_seed_compatibility(tmp_path):
    check_first_trim_preserves_all_values_coords_attrs_and_source(tmp_path, 30, 30)


def test_radar_seed_compatibility(tmp_path):
    check_first_trim_preserves_all_values_coords_attrs_and_source(tmp_path, 200, 200)


def test_untrimmed_copy_is_byte_identical(tmp_path):
    ds = scientific_fixture()
    source, destination = tmp_path / 'source.zarr', tmp_path / 'copy.zarr'
    write_source(ds, source, 297)
    result = run_seed(CANDIDATE, source, destination, ds.time.values[0], 300)
    assert result.returncode == 0, result.stderr
    assert tree_hash(source) == tree_hash(destination)


def test_empty_window_does_not_create_destination(tmp_path):
    ds = scientific_fixture()
    source, destination = tmp_path / 'source.zarr', tmp_path / 'empty.zarr'
    write_source(ds, source, 297)
    result = run_seed(CANDIDATE, source, destination, ds.time.values[-1] + np.timedelta64(1, 's'), 300)
    assert result.returncode == 0, result.stderr
    assert not destination.exists()


def load_appender():
    spec = importlib.util.spec_from_file_location('deployed_hatpro', HERE / 'deployed_hatpro_to_zarr.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_native_inputs(root, ds, label):
    # Native variable/dimension names consumed by the unchanged deployed appender.
    for out_key, native_key, suffix in [('LWP', 'LWP', 'LWP'), ('IWV', 'IWV', 'IWV'),
                                       ('SURF_T', 'Surf_T', 'MET')]:
        ds[[out_key]].rename({out_key: native_key}).to_netcdf(root / f'HATPROG5-AURORA-ICELAND_{label}.{suffix}.NC')
    for key, suffix in [('T_PROF', 'TPC'), ('T_PROF_CMP', 'CMP.TPC')]:
        profile = ds[[key]].rename({key: 'T_prof', 'range': 'altitude_layer'})
        profile['altitude'] = profile['altitude_layer']
        profile.to_netcdf(root / f'HATPROG5-AURORA-ICELAND_{label}.{suffix}.NC')


def test_real_native_appender_after_trim_then_next_trim(tmp_path):
    app = load_appender()
    native = tmp_path / 'raw'
    native.mkdir()
    # 1001 seeded samples, then 350 new samples plus a repeated overlap.
    all_data = scientific_fixture(1351)
    write_native_inputs(native, all_data.isel(time=slice(0, 1001)), '261021_104451')
    initial = app._build_dataset(native, chunk_time=297, chunk_range=48).load()
    source, destination = tmp_path / 'source.zarr', tmp_path / 'candidate.zarr'
    write_source(initial, source, 297)
    original = tree_hash(source)
    floor = initial.time.values[31] + np.timedelta64(1, 's')
    result = run_seed(CANDIDATE, source, destination, floor, 300)
    assert result.returncode == 0, result.stderr

    write_native_inputs(native, all_data.isel(time=slice(981, None)), '261021_132821')
    complete = app._build_dataset(native, chunk_time=300, chunk_range=48).load()
    expected = complete.sel(time=slice(floor, None))
    app.append_new(native, destination, chunk_time=300, chunk_range=48)
    with xr.open_zarr(destination, chunks={}) as actual:
        xr.testing.assert_identical(actual.load(), expected)
        assert actual.time.values[-1] == all_data.time.values[-1]
    assert tree_hash(source) == original

    next_floor = expected.time.values[31] + np.timedelta64(1, 's')
    next_destination = tmp_path / 'next.zarr'
    result = run_seed(CANDIDATE, destination, next_destination, next_floor, 300)
    assert result.returncode == 0, result.stderr
    with xr.open_zarr(next_destination, chunks={}) as actual:
        xr.testing.assert_identical(actual.load(), expected.sel(time=slice(next_floor, None)))


if __name__ == '__main__':
    # Stdlib runner for the deployed Python, which intentionally has no pytest.
    import inspect
    import tempfile
    import unittest

    def case(function):
        def run():
            with tempfile.TemporaryDirectory(prefix='seed-fixture-') as scratch:
                if 'tmp_path' in inspect.signature(function).parameters:
                    function(Path(scratch))
                else:
                    function()
        return unittest.FunctionTestCase(run, description=function.__name__)

    tests = [case(f) for name, f in list(globals().items())
             if name.startswith('test_') and callable(f)]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
