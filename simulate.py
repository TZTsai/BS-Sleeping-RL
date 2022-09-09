#!/usr/bin/env python
# %%
import torch
from utils import *
from agents import *
from config import get_config, DEBUG
from env import MultiCellNetEnv
import numpy as np
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, ctx
from dash.exceptions import PreventUpdate
from dash.dependencies import ClientsideFunction
# %reload_ext autoreload
# %autoreload 2

n_steps = 300
substeps = 4

# %%
def parse_env_args(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--area_size", type=float,
                        help="width of the square area in meters")
    parser.add_argument("--traffic_type", type=str,
                        help="type of traffic to generate")
    parser.add_argument("--start_time", type=str,
                        help="start time of the simulation")
    parser.add_argument("--accel_rate", type=float,
                        help="acceleration rate of the simulation")
    parser.add_argument("--act_interval", type=int,
                        help="number of simulation steps between two actions")
    return parser.parse_known_args(args)[0]

parser = get_config()
args = sys.argv + [
    "-T", str(n_steps),
    "--accel_rate", "60000",
    # "--start_time", "307800",
    "--start_time", "583200",
    "--traffic_type", "B",
    "--use_render",
    # "--use_dash", 
]
args, env_args = parser.parse_known_args(args)
env_args = parse_env_args(env_args)

# %%
set_log_level(args.log_level)

# seed
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
np.random.seed(args.seed)

# %% [markdown]
# ## Simulation Parameters

# %%
def get_env_kwargs(args):
    return {k: v for k, v in vars(args).items() if v is not None}

def get_latest_model_dir(args, env_args):
    run_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "results" \
        / args.env_name / env_args.traffic_type / args.algorithm_name / args.experiment_name
    assert run_dir.exists(), "Run directory does not exist: {}".format(run_dir)
    if args.model_dir is not None:
        return run_dir / args.model_dir
    p = 'wandb/run*/files' if args.use_wandb else 'run*/models'
    return max(run_dir.glob(p), key=os.path.getmtime)

# agent = FixedPolicy([5, 4, 4], 7)
# agent = RandomPolicy([5, 4, 4], 7)
env = MultiCellNetEnv(**get_env_kwargs(env_args), seed=args.seed)
env.print_info()
spaces = env.observation_space[0], env.cent_observation_space, env.action_space[0]
model_dir = get_latest_model_dir(args, env_args)
agent = MappoPolicy(args, *spaces, model_dir=model_dir)

# %%
obs, _, _ = env.reset()
if args.use_render:
    env.render()
    
def step_env(obs):
    actions = agent.act(obs, deterministic=False) if env.need_action else None
    obs, _, reward, done, _, _ = env.step(actions, substeps=substeps)
    if args.use_render:
        # if env._episode_steps > 20:
        #     env.render(mode='human')
        #     exit()
        env.render()
    return obs, reward[0], done

T = args.num_env_steps

def simulate(obs=obs):
    step_rewards = []
    for i in trange(T, file=sys.stdout):
        obs, reward, done = step_env(obs)
        step_rewards.append(reward)
    rewards = pd.Series(np.squeeze(step_rewards), name='reward')
    print(rewards.describe())
    if args.use_render and not args.use_dash:
        return env.animate()
    
simulate()

# %%
if not args.use_dash: exit()

app = Dash(__name__)

figure = env._figure
figure['layout'].pop('sliders')
figure['layout'].pop('updatemenus')

app.layout = html.Div([
    # html.H4('5G Network Simulation'),
    dcc.Graph(id="graph", figure=go.Figure(figure)),
    html.Div([
        html.Button('Play', id="run-pause", n_clicks=0, className='column'), 
        html.P(id="step-info", className='column')], className='row'),
    dcc.Interval(id='clock', interval=300),
    dcc.Slider(
        id='slider',
        min=0, max=T, step=1, value=0,
        marks={t: f'{t:.2f}' for t in np.linspace(0, T, num=6)},
    ),
    # dcc.Store(id='storage', data=env._figure)
])

# app.clientside_callback(
#     ClientsideFunction(namespace='clientside', function_name='update'),
#     Output("graph", "figure"),
#     Output("step-info", "children"),
#     Output("run-pause", "value"),
#     Output("slider", "value"),
#     Input("slider", "value"),
#     Input("run-pause", "n_clicks"),
#     Input("clock", "n_intervals"),
#     Input("storage", "data")
# )

@app.callback(
    Output("graph", "figure"),
    Output("step-info", "children"),
    Output("run-pause", "value"),
    Output("slider", "value"),
    Input("slider", "value"),
    Input("run-pause", "n_clicks"),
    Input("clock", "n_intervals"),
    Input("graph", "figure")
)
def update_plot(time, clicks, ticks, fig):
    running = clicks % 2
    if ctx.triggered_id != 'clock':
        raise PreventUpdate  # avoid loop
    elif not running:
        raise PreventUpdate
    t_max = len(fig['frames']) - 1
    if running and time < t_max:
        time += 1
    if time > t_max:
        time = t_max
    frame = fig['frames'][time]
    fig['data'] = frame['data']
    deep_update(fig['layout'], frame['layout'])
    text = "Step: {}  Time: {}".format(time, frame['name'])
    return fig, text, ('Stop' if running else 'Play'), time

# threading.Thread(target=simulate).start()
app.run_server(debug=True)
