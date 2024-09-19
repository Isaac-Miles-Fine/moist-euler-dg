from matplotlib import pyplot as plt
from moist_euler_dg.three_phase_euler_2D import ThreePhaseEuler2D
from moist_euler_dg.fortran_three_phase_euler_2D import FortranThreePhaseEuler2D
from moist_euler_dg.euler_2D import Euler2D
import numpy as np
import time
import os
import argparse
from mpi4py import MPI
import matplotlib.ticker as ticker

# test case parameters
domain_width = 10_000 # width of domain in metres
domain_height = 10_000 # height of domain in metres

p_surface = 1_00_000.0 # surface pressure in Pa
SST = 300.0 # sea surface temperature in Kelvin

cooling_rate = 10.0 / (3600 * 24) # cools 10 K per day


comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

parser = argparse.ArgumentParser()
parser.add_argument('--order', type=int, help='Polynomial order')
parser.add_argument('--nx', type=int, help='Number of cells in horizontal')
parser.add_argument('--nz', type=int, help='Number of cells in vertical')
parser.add_argument('--nproc', type=int, help='Number of procs', default=1)
parser.add_argument('--plot', action='store_true')
args = parser.parse_args()

# maps to define geometry these can be arbitrary - maps [0, 1]^2 to domain
zmap = lambda x, z: z * domain_height
xmap = lambda x, z: domain_width * (x - 0.5)

# number of cells in the vertical and horizontal direction
nz = args.nz
nx = args.nx

nproc = args.nproc
run_model = (not args.plot) # whether to run model - set false to just plot previous run

g = 9.81 # gravitational acceleration
poly_order = args.order # spatial order of accuracy
a = 0.5 # kinetic energy dissipation parameter
upwind = True

# experiment name - change this for new experiments!
exp_name_short = 'energy-forced-convection'
experiment_name = f'{exp_name_short}-nx-{nx}-nz-{nz}-p{poly_order}'
data_dir = os.path.join('data', experiment_name)
plot_dir = os.path.join('plots', experiment_name)

if rank == 0:
    print(f"---------- {exp_name_short} with nx={nx}, nz={nz}")
    if not os.path.exists(plot_dir): os.makedirs(plot_dir)
    if not os.path.exists(data_dir): os.makedirs(data_dir)

comm.barrier()


def neutrally_stable_dry_profile(solver):
    # create a hydrostatically balanced pressure and density profile
    dexdy = -g / (solver.cpd * SST)
    ex = 1 + dexdy * solver.zs
    p = p_surface * ex ** (solver.cpd / solver.Rd)
    density = p / (solver.Rd * ex * SST)

    return density, p


def initial_condition(solver):
    # initial wind is zero
    u = np.zeros_like(solver.xs)
    w = np.zeros_like(solver.xs)

    density, p = neutrally_stable_dry_profile(solver)

    # add arbitrary moisute profile
    qw = solver.rh_to_qw(0.95, p, density)  # choose 95% relative humidity

    # model must be initialized with entropy not temperature
    # so convert density, pressure, qw profile to a density, entropy, qw profile
    s = solver.entropy(density, qw, p=p)

    # can also do s = solver.entropy(density, qw, T=T) to use temperature profiles

    return u, w, density, s, qw


def energy_forcing(solver, state, dstatedt):
    u, w, h, s, q, T, mu, p, ie = solver.get_vars(state)
    dudt, dwdt, dhdt, dsdt, dqdt, *_ = solver.get_vars(dstatedt)

    max_E_forcing = 1.0 # Watts / m^3
    E_forcing = -max_E_forcing * 2 * ((solver.zs / solver.zs.max()) - 0.5)

    dEds = h * T
    # dEdt = dEds * dsdt =
    s_forcing = E_forcing / dEds

    dsdt += s_forcing


def energy_growth_from_forcing(solver):

    if solver.forcing is not None:
        state = solver.state

        dstatedt = np.zeros_like(state)
        solver.forcing(solver, state, dstatedt)

        u, w, h, s, q, T, mu, p, ie = solver.get_vars(state)
        u, w = solver.cov_to_phy(u, w)
        enthalpy = (ie + p) / h

        dudt, dwdt, dhdt, dsdt, dqdt, *_ = solver.get_vars(dstatedt)
        dudt, dwdt = solver.cov_to_phy(dudt, dwdt)

        dEdu = h * u
        dEdw = h * w
        dEdh = 0.5 * (u ** 2 + w ** 2) + enthalpy + solver.g * solver.zs
        dEds = h * T
        dEdq = h * mu

        dEdt = dEdu * dudt + dEdw * dwdt + dEdh * dhdt + dEds * dsdt + dEdq * dqdt
        dEdt = solver.integrate(dEdt)

    else:
        dEdt = 0.0

    return dEdt


# total run time
run_time = 9000

# save data at these times
tends = np.array([0.0, (1 / 3), (2 / 3), 1.0]) * run_time

time_list = []
energy_list = []
conservation_data_fp = os.path.join(data_dir, 'conservation_data.npy')

if run_model:
    solver = FortranThreePhaseEuler2D(xmap, zmap, poly_order, nx, g=g, cfl=0.5, a=a, nz=nz, upwind=upwind, nprocx=nproc, forcing=energy_forcing)
    u, v, density, s, qw = initial_condition(solver)

    np.random.seed(42 + rank)
    noise = 2 * (np.random.random(density.shape) - 0.5)
    density += 0.01 * density * noise
    solver.set_initial_condition(u, v, density, s, qw)

    dEdt_forcing = 0.0

    for i, tend in enumerate(tends):
        t0 = time.time()
        while solver.time < tend:
            dt = solver.get_dt()
            dEdt_incr = energy_growth_from_forcing(solver)
            if rank == 0:
                dEdt_forcing += dEdt_incr * dt

            time_list.append(solver.time)
            energy_list.append(solver.energy())
            solver.time_step(dt=dt)

        t1 = time.time()

        if rank == 0:
            print("Simulation time (unit less):", solver.time)
            print("Wall time:", time.time() - t0, '\n')

        solver.save(solver.get_filepath(data_dir, exp_name_short))

    if rank == 0:
        print('Relative energy change:', (energy_list[-1] - energy_list[0]) / energy_list[0])
        print("Bottom temp range:", solver.T[:, 0, :, 0].min(), solver.T[:, 0, :, 0].max())

        conservation_data = np.zeros((2, len(time_list)))
        conservation_data[0, :] = np.array(time_list)
        conservation_data[1, :] = np.array(energy_list)

        np.save(conservation_data_fp, conservation_data)

# plotting
elif rank == 0:
    plt.rcParams['font.size'] = '12'

    #
    solver_plot = ThreePhaseEuler2D(xmap, zmap, poly_order, nx, g=g, cfl=0.5, a=a, nz=nz, upwind=upwind, nprocx=1)
    # base state of the initial condition (excludes bubble perturbation)
    _, _, h0, s0, qw0 = initial_condition(solver_plot)
    qv0, ql0, qi0 = solver_plot.solve_fractions_from_entropy(h0, qw0, s0)

    def fmt(x, pos):
        a, b = '{:.2e}'.format(x).split('e')
        b = int(b)
        return r'${} \times 10^{{{}}}$'.format(a, b)

    plot_func_entropy = lambda s: s.project_H1(s.s)
    plot_func_density = lambda s: s.project_H1(s.h)
    plot_func_water = lambda s: s.project_H1(s.q)
    plot_func_vapour = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[0])
    plot_func_liquid = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[1])
    plot_func_ice = lambda s: s.project_H1(s.solve_fractions_from_entropy(s.h, s.q, s.s)[2])
    plot_func_u = lambda s: s.project_H1(s.u)
    plot_func_w = lambda s: s.project_H1(s.w)

    pfunc_list = [
        plot_func_entropy, plot_func_density,
        plot_func_water, plot_func_vapour, plot_func_liquid, plot_func_ice,
        plot_func_u, plot_func_w
    ]

    labels = ["entropy", "density", "water", "vapour", "liquid", "ice", "u", "w"]

    fig_list = [plt.subplots(2, 2, sharex=True, sharey=True) for _ in range(len(labels))]

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
            plt.tight_layout()

    for (fig, ax), label in zip(fig_list, labels):
        plot_name = f'{label}_{exp_name_short}'
        fp = solver_plot.get_filepath(plot_dir, plot_name, ext='png')
        fig.savefig(fp, bbox_inches="tight")

    conservation_data = np.load(conservation_data_fp)
    time_list = conservation_data[0, :]
    energy_list = conservation_data[1, :]

    energy_list = (energy_list - energy_list[0]) / energy_list[0]

    print('Energy error:', energy_list[-1])

    plt.figure()
    plt.plot(time_list, energy_list, label='Energy')
    plt.grid()
    plt.legend()
    # plt.yscale('symlog', linthresh=1e-15)
    fp = os.path.join(plot_dir, f'conservation_{exp_name_short}')
    plt.savefig(fp, bbox_inches="tight")