from matplotlib import pyplot as plt
from moist_euler_dg.three_phase_euler_2D import ThreePhaseEuler2D
from moist_euler_dg.euler_2D import Euler2D
import numpy as np
import time
import os
import argparse
from mpi4py import MPI
import matplotlib.ticker as ticker

# TODO:
# get thermo solver to converge at the same time

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

parser = argparse.ArgumentParser()
parser.add_argument('--nx', type=int, help='Number of cells in horizontal')
parser.add_argument('--nz', type=int, help='Number of cells in vertical')
parser.add_argument('--nproc', type=int, help='Number of procs', default=1)
parser.add_argument('--plot', action='store_true')
args = parser.parse_args()

# domain size
xlim = 50_000
zlim = 10_000
# maps to define geometry these can be arbitrary - maps [0, 1]^2 to domain
zmap = lambda x, z: z * zlim
xmap = lambda x, z: xlim * (x - 0.5)

# number of cells in the vertical and horizontal direction
nz = args.nz
nx = args.nx

nproc = args.nproc
run_model = (not args.plot) # whether to run model - set false to just plot previous run

g = 9.81 # gravitational acceleration
poly_order = 3 # spatial order of accuracy
a = 0.5 # kinetic energy dissipation parameter
upwind = True

# experiment name - change this for new experiments!
exp_name_short = 'ice-bubble'
experiment_name = f'{exp_name_short}-nx-{nx}-nz-{nz}-p{poly_order}'
data_dir = os.path.join('data', experiment_name)
plot_dir = os.path.join('plots', experiment_name)

if rank == 0:
    print(f"---------- Ice bubble with nx={nx}, nz={nz}")
    if not os.path.exists(plot_dir): os.makedirs(plot_dir)
    if not os.path.exists(data_dir): os.makedirs(data_dir)

comm.barrier()
#

def initial_condition(xs, ys, solver, pert):
    """
    :param xs:
    :param ys:
    :param solver:
    :param pert:
    :return:
    """

    u = 0 * ys
    v = 0 * ys

    dry_theta = 300
    dexdy = -g / (solver.cpd * dry_theta)
    ex = 1 + dexdy * ys
    p = 1_00_000.0 * ex ** (solver.cpd / solver.Rd)
    density = p / (solver.Rd * ex * dry_theta)

    qw = solver.rh_to_qw(0.95, p, density)
    qd = 1 - qw

    R = solver.Rd * qd + solver.Rv * qw
    T = p / (R * density)

    assert (qw <= solver.saturation_fraction(T, density)).all()

    rad_max = 2_000
    rad = np.sqrt(xs ** 2 + (ys - 1.0 * rad_max) ** 2)
    mask = rad < rad_max
    density -= mask * (pert * density / 300) * (np.cos(np.pi * (rad / rad_max) / 2) ** 2)

    T = p / (R * density)
    assert (qw <= solver.saturation_fraction(T, density)).all()

    s = qd * solver.entropy_air(T, qd, density)
    s += qw * solver.entropy_vapour(T, qw, density)

    qv, ql, qi = solver.solve_fractions_from_entropy(density, qw, s, verbose=True)

    return u, v, density, s, qw, qv, ql, qi


def cooling_and_sst_forcing(solver, state, dstatedt):
    u, w, h, s, q, T, mu, p, ie = solver.get_vars(state)
    dudt, dwdt, dhdt, dsdt, dqdt, *_ = solver.get_vars(dstatedt)

    # internal cooling
    dTdt = -1.0 / (3600 * 24)
    dsdt[:] += dTdt * solver.cvd / T

    # sst boundary forcing at bottom
    bottom_bdry_idx = solver.ip_vert_ext
    dsdt[bottom_bdry_idx] -= dTdt * solver.cvd / T[bottom_bdry_idx] #
    SST = 290.0
    dTdt = -(T[bottom_bdry_idx] - SST) / 600 # 10 mins relaxation time
    dsdt[bottom_bdry_idx] += dTdt * solver.cvd / T[bottom_bdry_idx]


# total run time
run_time = 600 / 3

# save data at these times
tends = np.array([0.0, (1 / 3), (2 / 3), 1.0]) * run_time

time_list = []
energy_list = []

if run_model:
    solver = ThreePhaseEuler2D(xmap, zmap, poly_order, nx, g=g, cfl=1.5, a=a, nz=nz, upwind=upwind, nprocx=nproc, forcing=cooling_and_sst_forcing)
    u, v, density, s, qw, qv, ql, qi = initial_condition(solver.xs, solver.zs, solver, pert=2.0)
    solver.set_initial_condition(u, v, density, s, qw)

    E0 = solver.energy()
    for i, tend in enumerate(tends):
        t0 = time.time()
        while solver.time < tend:
            # time_list.append(solver.time)
            # energy_list.append(solver.energy())
            solver.time_step()

        t1 = time.time()

        if rank == 0:
            print("Simulation time (unit less):", solver.time)
            print("Wall time:", time.time() - t0, '\n')

        solver.save(solver.get_filepath(data_dir, exp_name_short))
    E1 = solver.energy()

    if rank == 0:
        print('Rel energy change:', (E1 - E0) / E0)

# plotting
elif rank == 0:
    plt.rcParams['font.size'] = '12'

    #
    solver_plot = ThreePhaseEuler2D(xmap, zmap, poly_order, nx, g=g, cfl=0.5, a=a, nz=nz, upwind=upwind, nprocx=1)
    # base state of the initial condition (excludes bubble perturbation)
    _, _, _, s0, qw0, qv0, ql0, qi0 = initial_condition(solver_plot.xs, solver_plot.zs, solver_plot, pert=0.0)

    def fmt(x, pos):
        a, b = '{:.2e}'.format(x).split('e')
        b = int(b)
        return r'${} \times 10^{{{}}}$'.format(a, b)

    plot_func_entropy = lambda s: s.project_H1(s.s - s0)
    plot_func_density = lambda s: s.project_H1(s.h)
    plot_func_water = lambda s: s.project_H1(s.q - qw0)
    plot_func_vapour = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[0] - qv0)
    plot_func_liquid = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[1] - ql0)
    plot_func_ice = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[2] - qi0)

    fig_list = [plt.subplots(2, 2, sharex=True, sharey=True) for _ in range(6)]

    pfunc_list = [
        plot_func_entropy, plot_func_density,
        plot_func_water, plot_func_vapour, plot_func_liquid, plot_func_ice
    ]

    labels = ["entropy", "density", "water", "vapour", "liquid", "ice"]

    energy = []
    for i, tend in enumerate(tends):
        filepaths = [solver_plot.get_filepath(data_dir, exp_name_short, proc=i, nprocx=nproc, time=tend) for i in range(nproc)]
        solver_plot.load(filepaths)
        energy.append(solver_plot.integrate(solver_plot.energy()))

        for (fig, axs), plot_fun in zip(fig_list, pfunc_list):
            ax = axs[i // 2][i % 2]
            ax.tick_params(labelsize=8)
            im = solver_plot.plot_solution(ax, dim=2, plot_func=plot_fun)
            cbar = plt.colorbar(im, ax=ax, format=ticker.FuncFormatter(fmt))
            cbar.ax.tick_params(labelsize=8)

    for (fig, ax), label in zip(fig_list, labels):
        plot_name = f'{label}_{exp_name_short}'
        fp = solver_plot.get_filepath(plot_dir, plot_name, ext='png')
        fig.savefig(fp, bbox_inches="tight")

