import re
import logging
import glob
import numpy as np
from csld.util.string_utils import str2arr
from csld.interface_vasp import Poscar
from csld.structure import SupercellStructure
from csld.phonon.phonon import Phonon, NA_correction
from cssolve.csfit import csfit, predict_holdout
from csld.common_main import init_training

def fit_data(model, Amat, fval, setting, step, pdfout):
    """
    Fitting
    :param model: Lattice dynamics model
    :param Amat:
    :param fval:
    :param setting:
    :param step:
    :param pdfout:
    :return: optimal solution
    """
    if step <= 0:
        exit(0)
    elif step == 1:
        solutions = model.load_solution(setting['solution_in'],setting.getboolean('potential_coords_ijkl',False))
        if Amat is not None:
            err = [np.std(Amat.dot(solutions[i])-fval[:,0]) for i in range(solutions.shape[0])]
            ibest = np.argmin(err)
        else:
            ibest = 0
            if solutions.size <= 0:
                logging.error("ERROR: empty solution")
                exit(-1)
            if solutions.shape[0] > 1:
                logging.warning("More than 1 solutions found. Returning the first.")
        rel_err = 0
    elif step in [2, 3]:
        mulist = list(map(float, setting['mulist'].split()))
        submodels = [y.split() for x, y in setting.items() if re.match('submodel.*', x) is not None]
        submodels = [[x[0], list(map(int, x[1:]))] for x in submodels]
        uscale_list = list(map(float, setting['uscale_list'].split()))
        ldffscale_list = list(map(float, setting.get('ldffscale_list', '1').split()))
        knownsol = setting.get('solution_known', '')
        submodels = model.get_submodels(submodels, uscale_list, setting.getfloat('lr_pair_penalty',0.0),
                   ldffscale_list = ldffscale_list, knownsol=knownsol)

        ibest, solutions, rel_err = csfit(Amat, fval[:,0], 1, mulist,
                method=int(setting['method']),
                maxIter=int(setting['maxiter']),
                tol=float(setting['tolerance']),
                nSubset=int(setting['nsubset']),
                subsetsize=float(setting['subsetsize']),
                holdsize=float(setting['holdsize']),
                lbd=float(setting['lambda']),
# bcs options
                reweight=setting.getboolean('bcs_reweight', False),
                penalty=setting.get('bcs_penalty', 'arctan'),
                jcutoff=setting.getfloat('bcs_jcutoff',1E-7),
                sigma2=setting.getfloat('bcs_sigma2',-1.0),
                eta=setting.getfloat('bcs_eta',1E-3),
                fitf=setting.get('true_v_fit'),
                submodels=submodels, pdfout=pdfout)
        if step == 3:
            np.savetxt(setting['solution_out'], solutions)
            np.savetxt(setting['solution_out']+'_full', model.Cmat.T.dot(np.array(solutions)[:,:model.Cmat.shape[0]].T).T)
    else:
        print("ERROR: Unknown fit_step: ", step)
        exit(-1)
    if model.ldff is not None:
        model.ldff.plot_pairPES(solutions)
    print("+ Fitting done. Best solution", ibest)
    return ibest, solutions, rel_err


def save_pot(model, sol, setting, step, phonon):
    """
    :param model: the LD model
    :param sol the optimal solution vector
    :param settings:
    :return:
    """
    if step == 0:
        return
    scs = [y.split() for x, y in setting.items() if re.match(r'save_pot_cell.*', x)]
    combine= setting.getboolean('combine_improper', True)
    for i in scs:
        cell = np.array(list(map(int, i[1:])), dtype=int).reshape(3,3)
        model.save_fct(sol, i[0], cell, combine_improper=combine)
    if len(scs)>0:
        print("  + FCT saved to %d supercell(s)" % (len(scs)))

    # export_shengbte should be removed to disable (default), or "Nx Ny Nz 2 3 4 separated by space)"
    if 'export_shengbte' in setting.keys():
        numbers= list(map(int, setting['export_shengbte'].split()))
        orders = numbers[3:]
        use_old = setting.getboolean('original_shengbte_format', False)
        for ord in orders:
            if ord == 2:
            # note solution sol should already have been passed to phonon
                sc = SupercellStructure.from_scmat(model.prim, np.diag(numbers[:3]))
                if use_old:
                    phonon.export_hessian_forshengbte_original(sc)
                else:
                    phonon.export_hessian_forshengbte(sc)
            elif ord in [3,4]:
                if use_old:
                    model.save_fcshengbte_original(sol, ord)
                else:
                    model.save_fcshengbte(sol, ord)
            else:
                print("cannot force constants of %d order"%(ord))
        print("  + shengbte format exported")
    print("+ FCT export done")


def predict(model, sols, setting, step):
    """
    :param model:
    :param sol:
    :param setting:
    :param step:
    :return:
    """
    if step <= 0:
        return
    elif step in [1, 2, 3]:
        Amat, fval = init_training(model, setting, step, delForce=0)
    else:
        print("ERROR: Unknown pred_step: ", step)
        exit(-1)

    errs = []
    for i in range(len(sols)):
        err = predict_holdout(Amat, fval[:, 0], sols[i])
        err[2:4]+= fval[:,2:].T
        errs.append(err[0])
        print("  sol# %d: err= (%.2f%%) %f" % (i, err[0], err[1]))
        np.savetxt("%s_%d"%(setting['fval_out'],i), np.transpose(err[2:4]))
        if setting.getboolean('save_force_prediction', True) and setting['corr_type']=='f':
            supercells= [y.split() for x, y in setting.items() if re.match('traindat.*', x)]
            left=0
            f_all= np.reshape(err[3],(-1,3))
            for sc in supercells:
                nA = Poscar.from_file(sc[0]).structure.num_sites
                for subs in sc[1:]:
                    for f in sorted(glob.glob(subs)):
                        np.savetxt(f+'/force.txt_predicted', f_all[left:left+nA])
                        left+=nA
    print("+ Prediction done")
    return np.argmin(errs)

def phonon_step(model, sols, setting, step, pdfout, prim, return_eigen=False):
    """
    :param model:
    :param sol:
    :param setting:
    :param step:
    :return:
    """
    if step <= 0:
        return
    # set up NAC
    entries = [k for k, v in setting.items()]
    nac = NA_correction.from_dict(setting)
    unit = setting.get('unit', 'THz')
    etafac = setting.getfloat('etafac', 8.0)
    if step == 1:
        for i, sol in enumerate(sols):
            phonon = Phonon(prim, model, sol, pdfout, NAC=nac, etafac=etafac)
            cart=setting.getboolean("qpoint_cart")
            # logging.info('assuming '+('cartesian' if cart else 'fractional' )+' input q-points')
            # dispersion
            eigE = None
            if 'wavevector' in entries:
#                kpts = 'Auto' if setting['wavevector']=='Auto' else str2arr(setting['wavevector'], shape=(-1, 3))
                kpts = setting['wavevector']
                eigE = phonon.get_dispersion(kpts, unit=unit, cart=cart, no_gamma=setting.getboolean("no_gamma",True))
                print('  + phonon dispersion generated')
            if 'eigen_wavevector' in entries:
                kpts = str2arr(setting['eigen_wavevector']).reshape((-1,3))
                phonon.get_eig_e_vec(kpts, unit=unit, cart=cart)
                print('  + eigen vectors exported')
            if 'dos_grid' in entries:
                ngrid = str2arr(setting['dos_grid'], int)
                ismear = setting.getint('ismear', -1)
                epsilon= setting.getfloat('epsilon')
                # logging.info('    DOS ismear=%d smearing width=%f' %(ismear, epsilon))
                pdos = setting.getboolean('pdos', False)
                dos=phonon.get_dos(ngrid, int(setting['nE_dos']), ismear, epsilon, unit=unit, pdos=pdos,no_gamma=setting.getboolean("no_gamma",True))
                print('  + phonon DOS%s generated'% (' + partial DOS' if pdos else ''))
                if 'thermal_t_range' in entries:
                    t_rng = str2arr(setting['thermal_t_range'])
                    t_rng = np.arange(*(t_rng.tolist()))
                    Phonon.calc_thermal_QHA(dos, t_rng, setting.get('thermal_out','QHA.out'))
                    print('  + phonon thermal properties calculated')

            if 'debye_t_qfrac' in entries:
                d_T_grid=list(map(int,setting.get('debye_t_v_intgrid','20 20').split()))
                d_T_qfac=list(map(float,setting['debye_t_qfrac'].split()))
                d_T, d_v =phonon.debye_T(d_T_grid, *d_T_qfac)
                print("  + Debye T (K), v (m/s) at %s fractional q point =%.5f %.2f"%(d_T_qfac, d_T, d_v))
                print("  for each acoustic branch", phonon.debye_T(d_T_grid, *d_T_qfac, False)[0])

            if 'supercell' in entries:
                sc = SupercellStructure.from_scmat(prim, str2arr(setting['supercell'],int, (3,3)))
                if 'snapshot_t' in entries:
                    phonon.supercell_snapshot(sc, float(setting['snapshot_t']), int(setting.get('snapshot_n', '10')))
                    print('  + phonon thermalized snapshots exported')
                # frozen phonons
                if 'modes' in entries:
                    if 'reference_structure' in entries:
                        posRef = Poscar.from_file(setting['reference_structure']).structure
                        logging.info('Output structure will follow order of atoms in '+setting['reference_structure'])
                    else:
                        posRef = None
                    if 'mode_amplitude' not in entries:
                        logging.error('Need "mode_amplitude" e.g. "0.1 0.2" in settings to export phonon modes.')
                        exit(0)
                    phonon.export_phononmode(sc, str2arr(setting['mode_amplitude']),
                                             str2arr(setting['modes'],shape=(-1,4)), cart=cart, posRef=posRef)
                    print('   frozen phonon modes exported')
                # covariance matrix
                if 'covariance_matrix_t' in entries:
                    np.savetxt('covariance_matrix.out', phonon.covariance_matrix_in_supercell(sc, float(setting['covariance_matrix_t'])))
                # NOTE: moved invocation of this function to [export_potential] export_shengbte=...
                #if bool(setting.getboolean('fc2shengbte',False)):
                #    phonon.export_hessian_forshengbte(sc)
                #    print('   simplified force constants for shengbte exported')

    else:
        print("ERROR: Unknown phonon_step: ", step)
        exit(-1)

    print("+ Phonon done")
    if eigE is not None and return_eigen is True:
        return phonon, eigE
    else:
        return phonon
