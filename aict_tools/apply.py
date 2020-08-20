import numpy as np
import logging
from operator import le, lt, eq, ne, ge, gt
import h5py
import tables
from tqdm import tqdm

from .preprocessing import convert_to_float32, check_valid_rows
from .io import get_number_of_rows_in_table

log = logging.getLogger(__name__)


OPERATORS = {
    '<': lt, 'lt': lt,
    '<=': le, 'le': le,
    '==': eq, 'eq': eq,
    '=': eq,
    '!=': ne, 'ne': ne,
    '>': gt, 'gt': gt,
    '>=': ge, 'ge': ge,
}

text2symbol = {
    'lt': '<',
    'le': '<=',
    'eq': '==',
    'ne': '!=',
    'gt': '>',
    'ge': '>=',
}


def build_query(selection_config):
    queries = []
    for k, (o, v) in selection_config.items():
        o = text2symbol.get(o, o)

        queries.append(
            '{} {} {}'.format(k, o, '"' + v + '"' if isinstance(v, str) else v)
        )

    query = '(' + ') & ('.join(queries) + ')'
    return query


def predict_energy(df, model, log_target=False):
    df_features = convert_to_float32(df)
    valid = check_valid_rows(df_features)

    energy_prediction = np.full(len(df_features), np.nan)
    energy_prediction[valid] = model.predict(df_features.loc[valid].values)

    if log_target:
        energy_prediction[valid] = np.exp(energy_prediction[valid])

    return energy_prediction


def predict_disp(df, abs_model, sign_model, log_target=False):
    df_features = convert_to_float32(df)
    valid = check_valid_rows(df_features)

    disp_abs = abs_model.predict(df_features.loc[valid].values)
    disp_sign = sign_model.predict(df_features.loc[valid].values)

    if log_target:
        disp_abs = np.exp(disp_abs)

    disp_prediction = np.full(len(df_features), np.nan)
    disp_prediction[valid] = disp_abs * disp_sign

    return disp_prediction


def predict_separator(df, model):
    df_features = convert_to_float32(df)
    valid = check_valid_rows(df_features)

    score = np.full(len(df_features), np.nan)
    score[valid] = model.predict_proba(df_features.loc[valid].values)[:, 1]

    return score


def create_mask_h5py(
    infile,
    selection_config,
    n_events,
    key='events',
    start=None,
    end=None,
):
    start = start or 0
    end = min(n_events, end) if end else n_events

    n_selected = end - start
    mask = np.ones(n_selected, dtype=bool)

    # legacy support for dict of column_name -> [op, val]
    if isinstance(selection_config, dict):
        selection_config = [{k: v} for k, v in selection_config.items()]

    for c in selection_config:
        if len(c) > 1:
            raise ValueError('Expected dict with single entry column: [operator, value].')
        name, (operator, value) = list(c.items())[0]

        before = np.count_nonzero(mask)
        selection = OPERATORS[operator](
            infile[key][name][start:end],
            value
        )
        mask = np.logical_and(mask, selection)
        after = np.count_nonzero(mask)
        log.debug('Cut "{} {} {}" removed {} events'.format(
            name, operator, value, before - after
        ))

    return mask


def create_mask_table(
    table,
    selection_config,
    n_events,
    start=None,
    end=None,
):
    start = start or 0
    end = min(n_events, end) if end else n_events

    n_selected = end - start
    mask = np.ones(n_selected, dtype=bool)

    # legacy support for dict of column_name -> [op, val]
    if isinstance(selection_config, dict):
        selection_config = [{k: v} for k, v in selection_config.items()]

    for c in selection_config:
        if len(c) > 1:
            raise ValueError('Expected dict with single entry column: [operator, value].')
        name, (operator, value) = list(c.items())[0]

        before = np.count_nonzero(mask)
        if name not in table.colnames:
            raise KeyError(
                f'Cant perform selection based on {name} '
                'Column is missing from parameters table'
            )
        selection = OPERATORS[operator](
            table.col(name)[start:end],
            value
        )
        mask = np.logical_and(mask, selection)
        after = np.count_nonzero(mask)
        log.debug('Cut "{} {} {}" removed {} events'.format(
            name, operator, value, before - after
        ))

    return mask


def apply_cuts_h5py_chunked(
    input_path,
    output_path,
    selection_config,
    key='events',
    chunksize=100000,
    progress=True,
):
    '''
    Apply cuts defined in selection config to input_path and write result to
    outputpath. Apply cuts to chunksize events at a time.
    '''

    n_events = get_number_of_rows_in_table(input_path, key=key, )
    n_chunks = int(np.ceil(n_events / chunksize))
    log.debug('Using {} chunks of size {}'.format(n_chunks, chunksize))

    with h5py.File(input_path, 'r') as infile, h5py.File(output_path, 'w') as outfile:
        group = outfile.create_group(key)

        for chunk in tqdm(range(n_chunks), disable=not progress, total=n_chunks):
            start = chunk * chunksize
            end = min(n_events, (chunk + 1) * chunksize)

            mask = create_mask_h5py(
                infile, selection_config, n_events, key=key, start=start, end=end
            )

            for name, dataset in infile[key].items():
                if chunk == 0:
                    if dataset.ndim == 1:
                        group.create_dataset(
                            name, data=dataset[start:end][mask], maxshape=(None, )
                        )
                    elif dataset.ndim == 2:
                        group.create_dataset(
                            name, data=dataset[start:end, :][mask, :], maxshape=(None, 2)
                        )
                    else:
                        log.warning('Skipping not 1d or 2d column {}'.format(name))

                else:

                    n_old = group[name].shape[0]
                    n_new = np.count_nonzero(mask)
                    group[name].resize(n_old + n_new, axis=0)

                    if dataset.ndim == 1:
                        group[name][n_old:n_old + n_new] = dataset[start:end][mask]
                    elif dataset.ndim == 2:
                        group[name][n_old:n_old + n_new, :] = dataset[start:end][mask, :]
                    else:
                        log.warning('Skipping not 1d or 2d column {}'.format(name))


def apply_cuts_cta_dl1(
    input_path,
    output_path,
    selection_config
):
    '''
    Apply cuts from a selection config to a cta dl1 file and write results
    to output_path. This is done one row at a time, so chunksize wont do anything
    '''
    filters = tables.Filters(
        complevel=5,  # compression medium, tradeoff between speed and compression
        complib="blosc:zstd",  # use modern zstd algorithm
        fletcher32=True,  # add checksums to data chunks
    )
    n_rows_before = 0
    n_rows_after = 0
    with tables.open_file(input_path) as in_, tables.open_file(output_path, 'a', filters=filters) as out_:
        # perform cuts on the measured parameters
        remaining_showers = set()
        for table in in_.root.dl1.event.telescope.parameters:
            key = '/dl1/event/telescope/parameters'
            new_table = out_.create_table(
                key,
                table.name,
                table.description,
                createparents=True,
            )
            mask = create_mask_table(
                table,
                selection_config,
                n_events=len(table),
            )
            for row, match in zip(table.iterrows(), mask):
                n_rows_before += 1
                if match:
                    remaining_showers.add((row['obs_id'], row['event_id']))
                    new_table.append([row[:]])
                    n_rows_after += 1
        # copy the other tables disregarding events with no more observations
        for table in in_.walk_nodes():
            # skip groups, we create the parents anyway
            if not isinstance(table, tables.Table):
                continue
            # parameter tables were already processed
            if table._v_parent._v_pathname == '/dl1/event/telescope/parameters':
                continue
            new_table = out_.create_table(
                table._v_parent._v_pathname,
                table.name,
                table.description,
                createparents=True,
            )
            for row in table.iterrows():
                if 'event_id' in table.colnames:  # they dont appear individually
                    event = (row['obs_id'], row['event_id'])
                    if event not in remaining_showers:
                        continue
                new_table.append([row[:]])

        return n_rows_before, n_rows_after
