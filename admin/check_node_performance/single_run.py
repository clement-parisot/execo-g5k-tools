#!/usr/bin/env python

import os
import __main__
from string import Template
from execo import logger, TaktukRemote, SshProcess, default_connection_params, \
    ParallelActions
from execo.log import style
from execo_g5k import get_planning, compute_slots, get_resource_attributes, \
    find_first_slot, OarSubmission, oarsub, wait_oar_job_start, \
    get_cluster_site, deploy, Deployment, get_host_network_equipments, \
    get_host_site, get_host_attributes, get_host_shortname
from execo_g5k.planning import get_job_by_name
from execo_g5k.utils import g5k_args_parser, hosts_list
from execo_engine import copy_outputs

default_connection_params['user'] = 'root'
_sys_grep = '| grep "execution time" | awk \'{print $4}\' | cut -d / -f 1'


def main():
    """ """
    parser = g5k_args_parser(description="Reserve all the available nodes on "
                             "a cluster and check that nodes exhibits same "
                             "performance for cpu, disk, network",
                             cluster='stremi',
                             walltime='3:00:00',
                             loglevel=True,
                             job='check_node_perf',
                             deploy=True,
                             outdir=True)
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel('DEBUG')
    elif args.quiet:
        logger.setLevel('WARN')
    else:
        logger.setLevel('INFO')
    if not os.path.exists(args.outdir):
        os.mkdir(args.outdir)
    copy_outputs(args.outdir + '/' + parser.prog + '.log',
                 args.outdir + '/' + parser.prog + '.log')

    hosts = setup_hosts(get_hosts(args.job, args.cluster, args.walltime),
                        args.forcedeploy, args.nodeploy)
    for test in ['cpu', 'memory', 'fio', 'latency', 'bandwidth']:
        logger.info('STARTING %s BENCHMARK', style.user3(test))
        getattr(__main__, test)(hosts)
        logger.info('%s BENCHMARK DONE', style.user3(test))


def cpu(hosts, max_prime=100000):
    """ """
    n_core = get_host_attributes(hosts[0])['architecture']['smt_size']
    cmd = 'sysbench --num-threads=%s --test=cpu run --cpu-max-prime=%s %s' % \
        (n_core, max_prime, _sys_grep)
    logger.info('Launching CPU benchmark with \n%s', style.command(cmd))
    cpu_test = TaktukRemote(cmd, hosts).run()
    _print_bench_result(cpu_test, 'CPU')
    return True


def memory(hosts):
    """ """
    mem_size = get_host_attributes(hosts[0])['main_memory']['ram_size']
    cmd = 'sysbench --test=memory --memory-block-size=1M ' + \
        '--memory-total-size=' + str(mem_size) + ' run' + _sys_grep
    logger.info('Launching MEM benchmark with \n%s', style.command(cmd))
    mem_test = TaktukRemote(cmd, hosts).run()
    _print_bench_result(mem_test, 'MEM')
    return True


def fio(hosts):
    """ """
    n_core = get_host_attributes(hosts[0])['architecture']['smt_size']
    cmd = Template("cd /tmp && sysbench --num-threads=%s --test=fileio "
                   "--file-total-size=10G --file-test-mode=seqwr "
                   "$action $grep" % (n_core, ))
    logger.info('Preparing FIO benchmark')
    prepare = TaktukRemote(cmd.substitute(action='prepare', grep=""), hosts).run()
    if not prepare.ok:
        logger.error('Unable to prepare the data for FIO benchmark\n%s',
                     '\n'.join([p.host.address + ': ' + p.stout.strip()
                                for p in prepare.processes]))
        exit()
    logger.info('Launching FIO benchmark with \n%s',
                style.command(cmd.substitute(action='run', grep=_sys_grep)))
    run = TaktukRemote(cmd.substitute(action='run', grep=_sys_grep), hosts).run()
    _print_bench_result(run, 'FIO')
    logger.info('Cleaning FIO benchmark')
    clean = TaktukRemote(cmd.substitute(action='cleanup', grep=""), hosts).run()
    if not clean.ok:
        logger.error('Unable to cleant the data for FIO benchmark\n%s',
                     '\n'.join([p.host.address + ': ' + p.stout.strip()
                                for p in clean.processes]))
        exit()
    return True


def latency(hosts, n_ping=10):
    """ """
    dests = hosts + [str(get_host_network_equipments(hosts[0])[0]), get_host_site(hosts[0])]
    for c in hosts:
        log = c
        cmds = []
        for d in dests:
            if d != c:
                cmds.append('ping -c %s %s | tail -1| awk \'{print $4}\' | '
                            'cut -d \'/\' -f 2' % (n_ping, d))
        mes = TaktukRemote('{{cmds}}', [c] * len(cmds)).run()
        for p in mes.processes:
            logger.info('LAT %s->%s %s',
                        get_host_shortname(c),
                        get_host_shortname(d),
                        p.stdout.strip())


def bandwidth(hosts):
    """ """

#    print hosts


def setup_hosts(hosts, force_deploy, no_deploy):
    """ """
    logger.info('Deploying hosts')
    check = not force_deploy
    num_tries = int(not no_deploy)

    deployed, undeployed = deploy(Deployment(hosts=hosts, env_name="wheezy-x64-prod"),
                                  num_tries=num_tries,
                                  check_deployed_command=check)
    if len(undeployed) > 0:
        logger.warning('%s have not been deployed',
                       hosts_list(list(undeployed)))
    hosts = list(deployed)
    hosts.sort(key=lambda h: (h.split('.', 1)[0].split('-')[0],
                              int(h.split('.', 1)[0].split('-')[1])))

    taktuk_conf = ('-s', '-S',
                   '$HOME/.ssh/id_rsa:$HOME/.ssh/id_rsa,' +
                   '$HOME/.ssh/id_rsa.pub:$HOME/.ssh')
    conf_ssh = TaktukRemote('echo "Host *" >> /root/.ssh/config ;' +
                            'echo " StrictHostKeyChecking no" >> /root/.ssh/config; ',
                            hosts,
                            connection_params={'taktuk_options': taktuk_conf}).run()
    if not conf_ssh.ok:
        logger.error('Unable to configure SSH')
        exit()
    logger.info('Installing sysbench')
    cmd = 'apt-get update && apt-get install -y sysbench'
    install_sysbench = TaktukRemote(cmd, hosts).run()
    if not install_sysbench.ok:
        logger.error('Unable to install sysbench')
        exit()

    return hosts


def get_hosts(job_name, cluster, walltime):
    """ """
    job_id, site = get_job_by_name(job_name)
    if not job_id:
        job_id, site = _default_job(job_name, cluster, walltime)
        logger.info('Reservation done %s:%s', site, job_id)
    logger.info('Waiting for job start')
    wait_oar_job_start(job_id, site)
    job_info = get_resource_attributes('/sites/' + site +
                                       '/jobs/' + str(job_id))
    hosts = job_info['assigned_nodes']
    hosts.sort(key=lambda h: (h.split('.', 1)[0].split('-')[0],
                              int(h.split('.', 1)[0].split('-')[1])))
    logger.info('Hosts: %s', hosts_list(hosts))

    return hosts


def _print_bench_result(act, name):
    """ """
    for p in act.processes:
        logger.info('%s %s %s',
                    name,
                    style.host(get_host_shortname(p.host.address).ljust(15)),
                    p.stdout.strip())


def _default_job(job_name, cluster, walltime):
    """ """
    logger.info('No job running, making a reservation')
    wanted = {cluster: 0}
    planning = get_planning(wanted.keys())
    slots = compute_slots(planning, walltime)
    start_date, _, resources = find_first_slot(slots, wanted)
    jobs_specs = [(OarSubmission(resources='{cluster=\'%s\'}/nodes=%s' % 
                                 (cluster, resources[cluster]),
                                 job_type="deploy",
                                 walltime=walltime,
                                 reservation_date=start_date,
                                 name=job_name),
                   get_cluster_site(cluster))]
    return oarsub(jobs_specs)[0]


if __name__ == "__main__":
    main()
