"""
Functions for the construction and solving of the GRCS separation problem via ROsolver
"""
from pyomo.core.base.constraint import Constraint, ConstraintList
from pyomo.core.base.objective import (Objective,
                                       maximize,
                                       value)
from pyomo.core.base import Var, Param
from pyomo.common.collections import ComponentSet
from pyomo.common.dependencies import numpy as np
from pyomo.contrib.pyros.util import (ObjectiveType,
                                      get_time_from_solver,
                                      output_logger)
from pyomo.contrib.pyros.solve_data import SeparationResult
from pyomo.opt import TerminationCondition as tc
from pyomo.core.expr.current import (replace_expressions,
                                     identify_mutable_parameters,
                                     identify_variables)
from pyomo.contrib.pyros.util import get_main_elapsed_time
from pyomo.contrib.pyros.uncertainty_sets import Geometry
import os
from copy import deepcopy

def add_uncertainty_set_constraints(model, config):
    """
    Add inequality constraint(s) representing the uncertainty set.
    """
    model.util.uncertainty_set_constraint = \
        config.uncertainty_set.set_as_constraint(
            uncertain_params=model.util.uncertain_param_vars, model=model, config=config
        )

    config.uncertainty_set.add_bounds_on_uncertain_parameters(model, config)
    return


def make_separation_objective_functions(model, config):
    """
    Inequality constraints referencing control variables, state variables, or uncertain parameters
    must be separated against in separation problem.
    """
    performance_constraints = []
    for c in model.component_data_objects(Constraint, active=True, descend_into=True):
        _vars = ComponentSet(identify_variables(expr=c.expr))
        uncertain_params_in_expr = list(v for v in model.util.uncertain_param_vars.values() if v in _vars)
        state_vars_in_expr = list(v for v in model.util.state_vars if v in _vars)
        second_stage_variables_in_expr = list(v for v in model.util.second_stage_variables if v in _vars)
        if not c.equality and (uncertain_params_in_expr or state_vars_in_expr or second_stage_variables_in_expr):
            # This inequality constraint depends on uncertain parameters therefore it must be separated against
            performance_constraints.append(c)
    model.util.performance_constraints = performance_constraints
    model.util.separation_objectives = []
    map_obj_to_constr_names = {}

    if len(model.util.performance_constraints) == 0:
        raise ValueError("No performance constraints identified for the postulated robust optimization problem.")

    for idx, c in enumerate(performance_constraints):
        # Separation objective constraints standardized to be MAXIMIZATION of <= constraints
        c.deactivate()
        if c.upper is not None:
            # This is an <= constraint, maximized in separation
            obj = Objective(expr=c.body - c.upper, sense=maximize)
            map_obj_to_constr_names[c.name] = obj
            model.add_component("separation_obj_" + str(idx), obj)
            model.util.separation_objectives.append(obj)
        elif c.lower is not None:
            # This is an >= constraint, not supported
            raise ValueError("All inequality constraints in model must be in standard form (<= RHS)")

    model.util.map_obj_to_constr_names = map_obj_to_constr_names
    for obj in model.util.separation_objectives:
        obj.deactivate()

    return


def make_separation_problem(model_data, config):
    """
    Swap out uncertain param Param objects for Vars
    Add uncertainty set constraints and separation objectives
    """
    separation_model = model_data.original.clone()

    uncertain_params = separation_model.util.uncertain_params
    separation_model.util.uncertain_param_vars = param_vars = Var(range(len(uncertain_params)))
    map_new_constraint_list_names_to_original_con_names = {}

    if config.objective_focus is ObjectiveType.worst_case:
        separation_model.util.zeta = Param(initialize=0, mutable=True)
        constr = Constraint(expr= separation_model.first_stage_objective + separation_model.second_stage_objective + separation_model.const_obj_term
                                  <= separation_model.util.zeta)
        separation_model.add_component("epigraph_constr", constr)

    substitution_map = {}
    #Separation problem initialized to nominal uncertain parameter values
    for idx, var in enumerate(list(param_vars.values())):
        param = uncertain_params[idx]
        var.value = param.value
        substitution_map[id(param)] = var

    separation_model.util.new_constraints = constraints = ConstraintList()

    uncertain_param_set = ComponentSet(uncertain_params)
    for c in separation_model.component_data_objects(Constraint, active=True):
        if any(v in uncertain_param_set for v in identify_mutable_parameters(c.expr)):
            if c.equality:
                constraints.add(
                    replace_expressions(expr=c.lower, substitution_map=substitution_map) ==
                    replace_expressions(expr=c.body, substitution_map=substitution_map))
            elif c.lower is not None:
                constraints.add(
                    replace_expressions(expr=c.lower, substitution_map=substitution_map) <=
                    replace_expressions(expr=c.body, substitution_map=substitution_map))
            elif c.upper is not None:
                constraints.add(
                    replace_expressions(expr=c.upper, substitution_map=substitution_map) >=
                    replace_expressions(expr=c.body, substitution_map=substitution_map))
            else:
                raise ValueError("Unable to parse constraint for building the separation problem.")
            c.deactivate()
            map_new_constraint_list_names_to_original_con_names[
                constraints[constraints.index_set().last()].name] = c.name

    separation_model.util.map_constr_list_names_to_original_con_names = map_new_constraint_list_names_to_original_con_names

    # === Add objectives first so that the uncertainty set
    #     Constraints do not get picked up into the set
    #	  of performance constraints which become objectives
    make_separation_objective_functions(separation_model, config)
    add_uncertainty_set_constraints(separation_model, config)

    return separation_model


def get_all_sep_objective_values(model_data, config):
    """
    Returns all violations from separation
    """
    list_of_violations_across_objectives = []
    for o in model_data.separation_model.util.separation_objectives:
        try:
            list_of_violations_across_objectives.append(value(o.expr))
        except:
            for v in model_data.separation_model.util.first_stage_variables:
                config.progress_logger.info(v.name + " " + str(v.value))
            for v in model_data.separation_model.util.second_stage_variables:
                config.progress_logger.info(v.name + " " + str(v.value))
            raise ArithmeticError(
                "Objective function " + str(o) + " led to a math domain error. "
                 "Does this objective (meaning, its parent performance constraint) "
                 "contain log(x)  or 1/x functions or others with tricky domains?")
    return list_of_violations_across_objectives


def get_index_of_max_violation(model_data, config, solve_data_list):

    is_discrete_scenarios = True if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS else False
    matrix_dim=0
    indices_of_violating_realizations = []
    indices_of_violating_realizations_and_scenario = {}
    if is_discrete_scenarios:
        # There are num_scenarios by num_sep_objectives solutions to consider, take the worst-case per sep_objective
        for idx, row in enumerate(solve_data_list):
            if any(v.found_violation for v in row):
                matrix_dim+=1
                if len([v for v in row if v.found_violation]) > 1:
                    max_val, violation_idx = max(
                        (val.list_of_scaled_violations[idx], the_index) for the_index, val in enumerate(row)
                    )
                else:
                    for elem in row:
                        if elem.found_violation:
                            violation_idx = row.index(elem)
                indices_of_violating_realizations.append(idx)
                indices_of_violating_realizations_and_scenario[idx] = violation_idx
    else:
        matrix_dim = len(list(result for solve_list in solve_data_list for result in solve_list if result.found_violation == True))
        idx_j = 0
        indices_of_violating_realizations.extend(i for i,x in enumerate(solve_data_list) if x[idx_j].found_violation==True)

    if matrix_dim == 0:
        return 0, 0 # Just a dummy index...

    matrix_of_violations = np.zeros(shape=(matrix_dim, len(model_data.separation_model.util.performance_constraints)))
    violation_dict = {}
    if is_discrete_scenarios:
        violation_dict = indices_of_violating_realizations_and_scenario
    else:
        for k in indices_of_violating_realizations:
            for l in range(len(solve_data_list[k])):
                if solve_data_list[k][l].found_violation:
                    violation_dict[k] = l
    for i in range(matrix_dim):
        for j in range(len(model_data.separation_model.util.performance_constraints)):
            if is_discrete_scenarios:
                idx_max_violation_from_scenario = violation_dict[indices_of_violating_realizations[i]]
                matrix_of_violations[i][j] = max(
                    solve_data_list[indices_of_violating_realizations[i]][idx_max_violation_from_scenario].list_of_scaled_violations[j], 0)
            else:
                matrix_of_violations[i][j] = max(solve_data_list[indices_of_violating_realizations[i]][0].list_of_scaled_violations[j], 0)

    sums = []
    for i in range(matrix_of_violations.shape[1]):
        sum = 0
        column = matrix_of_violations[:, i]
        for j in range(len(column)):
            sum += column[j]
        sums.append(sum)
    max_value = max(sums)
    idx_i = sums.index(max_value)

    if is_discrete_scenarios:
        idx_j = violation_dict[idx_i]

    return idx_i, idx_j


def solve_separation_problem(model_data, config):

    # Timing variables
    global_solve_time = 0
    local_solve_time = 0

    # List of objective functions
    objectives_map = model_data.separation_model.util.map_obj_to_constr_names
    constraint_map_to_master = model_data.separation_model.util.map_constr_list_names_to_original_con_names

    # Add additional or remaining separation objectives to the dict
    # (those either not assigned an explicit priority or those added by Pyros for ssv bounds)
    sep_priority_dict = config.separation_priority_order
    for perf_con in model_data.separation_model.util.performance_constraints:
        if perf_con.name not in sep_priority_dict.keys():
            sep_priority_dict[perf_con.name] = 0

    # "Bin" the objectives based on priorities
    sorted_unique_priorities = sorted(list(set(sep_priority_dict.values())), reverse=True)

    for is_global in (False, True):
        solver = config.global_solver if \
            (is_global or config.bypass_local_separation) else config.local_solver
        solve_data_list = []
        list_of_deterministic_constraint_names = list(c.local_name for c in
                                        model_data.master_nominal_scenario.component_data_objects(Constraint, descend_into=True))
        for val in sorted_unique_priorities:
            # Descending ordered by value
            # The list of performance constraints with this priority
            perf_constraints = [constr_name for constr_name, priority in sep_priority_dict.items() if priority == val]
            for perf_con in perf_constraints:
                #config.progress_logger.info("Separating constraint " + str(perf_con))
                try:
                    separation_obj = objectives_map[perf_con]
                except:
                    raise ValueError("Error in mapping separation objective to its master constraint form.")
                separation_obj.activate()

                if perf_con in list_of_deterministic_constraint_names:
                    nom_constraint = perf_con
                else:
                    nom_constraint = constraint_map_to_master[perf_con]

                try:
                    model_data.master_nominal_scenario_value = value(model_data.master_nominal_scenario.find_component(nom_constraint))
                except:
                    raise ValueError("Unable to access nominal scenario value for the constraint " + str(nom_constraint))

                if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                    solve_data_list.append(discrete_solve(model_data=model_data, config=config,
                                                                       solver=solver, is_global=is_global))
                    if all(s.termination_condition in globally_acceptable for
                           sep_soln_list in solve_data_list for s in sep_soln_list) or \
                            (is_global == False and all(s.termination_condition in locally_acceptable for
                                                        sep_soln_list in solve_data_list for s in sep_soln_list)):
                        exit_separation_loop = False
                    else:
                        exit_separation_loop = True
                else:
                    solve_data = SeparationResult()
                    exit_separation_loop = solver_call_separation(model_data=model_data,
                                           config=config,
                                           solver=solver,
                                           solve_data=solve_data,
                                           is_global=is_global)
                    solve_data_list.append([solve_data])

                # === Keep track of total solve times
                if is_global or config.bypass_local_separation:
                    if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                        for sublist in solve_data_list:
                            for s in sublist:
                                global_solve_time += get_time_from_solver(s.results)
                    else:
                        global_solve_time += get_time_from_solver(solve_data.results)
                else:
                    if config.uncertainty_set.geometry == Geometry.DISCRETE_SCENARIOS:
                        for sublist in solve_data_list:
                            for s in sublist:
                                local_solve_time += get_time_from_solver(s.results)
                    else:
                        local_solve_time += get_time_from_solver(solve_data.results)

                # === Terminate for timing
                if exit_separation_loop:
                    return solve_data_list, [], [], is_global, local_solve_time, global_solve_time
                separation_obj.deactivate()

        # Do we return?
        # If their are multiple violations in this bucket, pick the worst-case
        idx_i, idx_j = get_index_of_max_violation(model_data=model_data, config=config,
                                                              solve_data_list=solve_data_list)

        violating_realizations = [v for v in solve_data_list[idx_i][idx_j].violating_param_realization]
        violations = solve_data_list[idx_i][idx_j].list_of_scaled_violations

        if any(s.found_violation for solve_list in solve_data_list for s in solve_list):
            #config.progress_logger.info(
            #	"Violation found in constraint %s with realization %s" % (
            #	list(objectives_map.keys())[idx_i], violating_realizations))
            return solve_data_list, violating_realizations, violations, is_global, local_solve_time, global_solve_time
        else:
            if not is_global:
                config.progress_logger.info("No violations identified. Next is global certification separation.")
            else:
                config.progress_logger.info("No violations identified via global solve. Exiting algorithm.")

    return solve_data_list, [], [], is_global, local_solve_time, global_solve_time


def get_absolute_tol(model_data, config):
    nom_value = model_data.master_nominal_scenario_value
    denom = float(max(1, abs(nom_value)))
    tol = config.robust_feasibility_tolerance
    return denom * tol, nom_value


def is_violation(model_data, config, solve_data):

    nom_value = model_data.master_nominal_scenario_value
    denom = float(max(1, abs(nom_value)))
    tol = config.robust_feasibility_tolerance
    active_objective = next(model_data.separation_model.component_data_objects(Objective, active=True))

    if value(active_objective)/denom > tol:
        violating_param_realization = list(
            p.value for p in list(model_data.separation_model.util.uncertain_param_vars.values())
        )
        list_of_violations = get_all_sep_objective_values(model_data=model_data, config=config)
        solve_data.violating_param_realization = violating_param_realization
        solve_data.list_of_scaled_violations = [l/denom for l in list_of_violations]
        solve_data.found_violation = True
        return True
    else:
        violating_param_realization = list(
            p.value for p in list(model_data.separation_model.util.uncertain_param_vars.values())
        )
        list_of_violations = get_all_sep_objective_values(model_data=model_data, config=config)
        solve_data.violating_param_realization = violating_param_realization
        solve_data.list_of_scaled_violations = [l/denom for l in list_of_violations]
        solve_data.found_violation = False
        return False


def initialize_separation(model_data, config):
    """
    Fix the separation problem variables to the optimal master problem solution
    In the case of the static_approx decision rule, control vars are treated
    as design vars are are therefore fixed to the optimum from the master.
    """
    if config.uncertainty_set.geometry != Geometry.DISCRETE_SCENARIOS:
        for idx, p in list(model_data.separation_model.util.uncertain_param_vars.items()):
            p.value = config.nominal_uncertain_param_vals[idx]
            p.unfix()
    for idx, v in enumerate(model_data.separation_model.util.first_stage_variables):
        v.fix(model_data.opt_fsv_vals[idx])

    for idx, c in enumerate(model_data.separation_model.util.second_stage_variables):
        c.value = model_data.opt_ssv_vals[idx]

    for c in model_data.separation_model.util.second_stage_variables:
        if config.decision_rule_order != 0:
            c.unfix()
        else:
            c.fix()
    if config.decision_rule_order == 0:
        for v in model_data.separation_model.util.decision_rule_eqns:
            v.deactivate()
        for v in model_data.separation_model.util.decision_rule_vars:
            v.fix()

    return

locally_acceptable = {tc.optimal, tc.locallyOptimal, tc.globallyOptimal}
globally_acceptable = {tc.optimal, tc.globallyOptimal}

def solver_call_separation(model_data, config, solver, solve_data, is_global):
    """
    Solve the separation problem.
    """
    save_dir = config.subproblem_file_directory

    if is_global or config.bypass_local_separation:
        backup_solvers = deepcopy(config.backup_global_solvers)
    else:
        backup_solvers = deepcopy(config.backup_local_solvers)
    backup_solvers.insert(0, solver)
    solver_status_dict = {}
    while len(backup_solvers) > 0:
        solver = backup_solvers.pop(0)
        nlp_model = model_data.separation_model

        # === Fix to Master solution
        initialize_separation(model_data, config)

        if not solver.available():
            raise RuntimeError("Solver %s is not available." %
                               solver)
        try:
            results = solver.solve(nlp_model, tee=config.print_subsolver_progress_to_screen)
        except ValueError as err:
            if 'Cannot load a SolverResults object with bad status: error' in str(err):
                solve_data.termination_condition = tc.error
                return True
            else:
                raise
        solver_status_dict[str(solver)] = results.solver.termination_condition
        solve_data.termination_condition = results.solver.termination_condition
        solve_data.results = results
        # === Process result
        is_violation(model_data, config, solve_data)

        if solve_data.termination_condition in globally_acceptable or \
                (not is_global and solve_data.termination_condition in locally_acceptable):
            return False

        # Else: continue with backup solvers unless we have hit time limit or not found any acceptable solutions
        elapsed = get_main_elapsed_time(model_data.timing)
        if elapsed >= config.pyros_time_limit and config.pyros_time_limit != -1:
            return True

    # === Write this instance to file for user to debug because this separation instance did not return an optimal solution
    if save_dir:
        objective = str(list(nlp_model.component_data_objects(Objective, active=True))[0].name)
        name = os.path.join(save_dir, config.uncertainty_set.type + "_" + nlp_model.name + "_separation_" + str(
            model_data.iteration) + "_obj_" + objective + ".bar")
        nlp_model.write(name, format="bar")
        output_logger(config=config, separation_error=True, filename=name, iteration=model_data.iteration, objective=objective,
                      status_dict=solver_status_dict)
    return True


def discrete_solve(model_data, config, solver, is_global):
    """
    Loops over discrete scenarios, solving square problem to determine constraint violation in separation objective.
    """
    # Constraint are grouped by dim(uncertain_param) groups for each scenario in D
    solve_data_list = []
    conlist = model_data.separation_model.util.uncertainty_set_constraint
    chunk_size = len(model_data.separation_model.util.uncertain_param_vars)
    constraints = list(conlist.values())
    conlist.deactivate()

    for i in range(0, len(constraints), chunk_size):
        chunk = constraints[i:i + chunk_size]
        for idx, con in enumerate(chunk):
            con.activate()
            model_data.separation_model.util.uncertain_param_vars[idx].fix(con.lower)
            con.deactivate()
        solve_data = SeparationResult()
        solver_call_separation(model_data=model_data,
                               config=config,
                               solver=solver,
                               solve_data=solve_data,
                               is_global=is_global)
        solve_data_list.append(solve_data)
        for con in chunk:
            con.deactivate()

    return solve_data_list


