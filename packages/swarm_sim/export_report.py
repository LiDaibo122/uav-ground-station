"""Export actual MAVLink traces as a standalone scientific plot and CSV."""
import argparse
import csv
import json
from pathlib import Path
import shutil

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from .sitl import DEFAULT_RUN, ROOT


def export(run_dir=DEFAULT_RUN, prefix='flight'):
    run_dir = Path(run_dir)
    report = json.loads((run_dir/'flight-report.json').read_text())
    samples = [json.loads(line) for line in (run_dir/'telemetry.jsonl').read_text().splitlines()]
    samples = [s for s in samples if s['timestamp'] <= report.get('completed_at', float('inf'))]
    output = ROOT/'reports'
    output.mkdir(exist_ok=True)
    shutil.copyfile(run_dir/'flight-report.json', output/(prefix+'-verification.json'))
    with (output/(prefix+'-telemetry.csv')).open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    fig, (ax, altitude) = plt.subplots(2, 1, figsize=(10, 9), gridspec_kw={'height_ratios': [2, 1]})
    colors = ['#18724e', '#c07a17', '#345eac']
    t0 = samples[0]['timestamp']
    for i, (uid, route) in enumerate(report['allocation']['assignments'].items()):
        data = [s for s in samples if s['id'] == uid]
        color = colors[i % len(colors)]
        ax.plot([s['east_m'] for s in data], [s['north_m'] for s in data], color=color, label=uid, lw=1.7)
        altitude.plot([s['timestamp']-t0 for s in data], [s['relative_alt_m'] for s in data], color=color, label=uid)
        for task_id in route:
            task = next(t for t in report['tasks'] if t['id'] == task_id)
            ax.add_patch(plt.Circle((task['east_m'], task['north_m']), 85, fill=False, color=color, alpha=.4, ls='--'))
            ax.scatter(task['east_m'], task['north_m'], marker='x', color=color, s=40)
            ax.annotate(task_id, (task['east_m'], task['north_m']), xytext=(7, 12), textcoords='offset points', fontsize=8, color=color)
    ax.set(title='Actual PX4 fixed-wing trajectories following CBBA allocation', xlabel='East from simulation origin (m)', ylabel='North (m)')
    ax.set_aspect('equal', adjustable='datalim')
    ax.legend(loc='upper left')
    ax.grid(alpha=.2)
    altitude.set(xlabel='Wall-clock seconds after arming', ylabel='Altitude above home (m)')
    altitude.grid(alpha=.2)
    title = 'PASS' if report['success'] else 'INCOMPLETE'
    fig.suptitle('%s | %d aircraft / %d inspection points | Flat Gazebo world' % (title,len(report['aircraft']),len(report['tasks'])), fontsize=14)
    fig.text(.5, .015, 'MAVLink measurements; no real mountain collision model or dynamic avoidance in this experiment.', ha='center', fontsize=9, color='#666666')
    fig.tight_layout(rect=[0, .035, 1, .96])
    fig.savefig(str(output/(prefix+'-trajectories.png')), dpi=180)
    fig.savefig(str(output/(prefix+'-trajectories.svg')))
    plt.close(fig)
    print('Exported', output/(prefix+'-trajectories.png'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--prefix',default='flight')
    args = parser.parse_args()
    export(args.run,args.prefix)
