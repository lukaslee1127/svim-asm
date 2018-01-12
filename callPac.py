from __future__ import print_function

__version__ = '0.1'
__author__ = 'David Heller'

import sys
import argparse
import os
import re
from subprocess import call, Popen, PIPE
from collections import defaultdict
from time import strftime, localtime
import pickle
import gzip
import logging

import pysam
from Bio import SeqIO

from callPacParams import callPacParams
from callPacFull import analyze_full_read_indel, analyze_full_read_segments
from callPacTails import analyze_pair_of_read_tails
from callPacPost import post_processing


def parse_arguments():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description="""callPac is a tool for accurate detection of structural variants (SVs).""")
    subparsers = parser.add_subparsers(help='modes', dest='sub')
    parser.add_argument('--version', '-v', action='version', version='%(prog)s {version}'.format(version=__version__))

    parser_bam = subparsers.add_parser('load', help='Load existing .obj file from working directory')
    parser_bam.add_argument('working_dir', type=str, help='working directory')

    parser_bam = subparsers.add_parser('alignment', help='Detect SVs from an existing alignment')
    parser_bam.add_argument('working_dir', type=os.path.abspath, help='working directory')
    parser_bam.add_argument('bam_file', type=argparse.FileType('r'), help='SAM/BAM file with aligned long reads (must be query-sorted)')
    parser_bam.add_argument('--skip_indel', action='store_true', help='disable indel part')
    parser_bam.add_argument('--skip_segment', action='store_true', help='disable segment part')

    parser_bam.add_argument('--tail_min_mapq', type=int, default=30, help='minimum mapping quality')

    parser_fasta = subparsers.add_parser('reads', help='Detect SVs from raw reads')
    parser_fasta.add_argument('working_dir', type=str, help='working directory')
    parser_fasta.add_argument('reads_file', type=str, help='Read file (FASTA, FASTQ, gzipped FASTA and FASTQ)')
    parser_fasta.add_argument('genome', type=str, default="/scratch/cluster/heller_d/genomes/hg19/hg19.fa", help='Reference genome file (FASTA)')

    parser_fasta.add_argument('--skip_kmer', action='store_true', help='disable kmer counting')
    parser_fasta.add_argument('--skip_indel', action='store_true', help='disable indel part')
    parser_fasta.add_argument('--skip_segment', action='store_true', help='disable segment part')
    parser_fasta.add_argument('--read_name', type=str, default="all", help='read name filter (default: all)')

    parser_fasta.add_argument('--cores', type=int, default=4, help='number of CPU cores to utilize for alignment')
    parser_fasta.add_argument('--tail_span', type=int, default=1000, help='length of read tails')
    parser_fasta.add_argument('--tail_min_mapq', type=int, default=30, help='minimum mapping quality')
    parser_fasta.add_argument('--tail_min_deviation', type=float, default=-0.1, help='minimum deviation')
    parser_fasta.add_argument('--tail_max_deviation', type=float, default=0.2, help='maximum deviation')
    parser_fasta.add_argument('--count_win_size', type=int, default=50, help='window size for k-mer counting')
    parser_fasta.add_argument('--count_k', type=int, default=7, help='k for k-mer counting')
    parser_fasta.add_argument('--count_band', type=float, default=0.5, help='band width')
    parser_fasta.add_argument('--stretch_threshold', type=int, default=7, help='z-score threshold')
    parser_fasta.add_argument('--stretch_tolerance', type=int, default=2, help='tolerance for stretch finding')
    parser_fasta.add_argument('--stretch_min_length', type=int, default=3, help='minimum stretch length')
    parser_fasta.add_argument('--path_constant_gap_cost', type=float, default=0, help='constant gap cost for path finding')
    parser_fasta.add_argument('--path_linear_gap_cost', type=float, default=0, help='linear gap cost for path finding')
    parser_fasta.add_argument('--path_convex_gap_cost', type=float, default=3, help='convex gap cost for path finding')
    parser_fasta.add_argument('--path_root_gap_cost', type=float, default=0, help='root gap cost for path finding')
    parser_fasta.add_argument('--path_tolerance', type=int, default=2, help='tolerance for overlapping segments')
    parser_fasta.add_argument('--align_costs_match', type=int, default=3, help='match cost for alignment')
    parser_fasta.add_argument('--align_costs_mismatch', type=int, default=-12, help='mismatch cost for alignment')
    parser_fasta.add_argument('--align_costs_gap', type=int, default=-12, help='gap cost for alignment')
    return parser.parse_args()


def parse_sam_file(sam, contig):
    """Parses a SAM file and returns a dict of reads (list of alignments for each read) for a given reference contig"""
    alns = sam.fetch(reference=contig)
    aln_dict = defaultdict(list)
    for aln in alns:
        aln_dict[aln.query_name].append(aln)
    return aln_dict


def guess_file_type(reads_path):
    if reads_path.endswith(".fa") or reads_path.endswith(".fasta") or reads_path.endswith(".FA"):
        logging.info("Recognized reads file as FASTA format.")
        return "fasta"
    elif reads_path.endswith(".fq") or reads_path.endswith(".fastq") or reads_path.endswith(".FQ"):
        logging.info("Recognized reads file as FASTQ format.")
        return "fastq"
    elif reads_path.endswith(".fa.gz") or reads_path.endswith(".fasta.gz") or reads_path.endswith(".fa.gzip") or reads_path.endswith(".fasta.gzip"):
        logging.info("Recognized reads file as gzipped FASTA format.")
        return "fasta_gzip"
    elif reads_path.endswith(".fq.gz") or reads_path.endswith(".fastq.gz") or reads_path.endswith(".fq.gzip") or reads_path.endswith(".fastq.gzip"):
        logging.info("Recognized reads file as gzipped FASTQ format.")
        return "fastq_gzip"
    elif reads_path.endswith(".fa.fn"):
        logging.info("Recognized reads file as FASTA file list format.")
        return "list" 
    else:
        logging.error("Unknown file ending of file {0}. Exiting.".format(reads_path))
        return "unknown"

def create_tail_files(working_dir, reads_path, reads_type, span):
    """Create FASTA files with read tails and full reads if they do not exist."""
    if not os.path.exists(working_dir):
        logging.error("Given working directory does not exist")
        sys.exit()

    reads_file_prefix = os.path.splitext(os.path.basename(reads_path))[0]

    if not os.path.exists("{0}/{1}_left.fa".format(working_dir, reads_file_prefix)):
        left_file = open("{0}/{1}_left.fa".format(working_dir, reads_file_prefix), 'w')
        write_left = True
    else:
        write_left = False
        logging.warning("FASTA file for left tails exists. Skip")

    if not os.path.exists("{0}/{1}_right.fa".format(working_dir, reads_file_prefix)):
        right_file = open("{0}/{1}_right.fa".format(working_dir, reads_file_prefix), 'w')
        write_right = True
    else:
        write_right = False
        logging.warning("FASTA file for right tails exists. Skip")

    if reads_type == "fasta_gzip" or reads_type == "fastq_gzip":
        full_reads_path = "{0}/{1}.fa".format(working_dir, reads_file_prefix)
        if not os.path.exists(full_reads_path):
            full_file = open(full_reads_path, 'w')
            write_full = True
        else:
            write_full = False
            logging.warning("FASTA file for full reads exists. Skip")
    else:
        full_reads_path = reads_path
        write_full = False

    if write_left or write_right or write_full:
        if reads_type == "fasta" or reads_type == "fastq":
            reads_file = open(reads_path, "r")
        elif reads_type == "fasta_gzip" or reads_type == "fastq_gzip":
            reads_file = gzip.open(reads_path, "rb")
        
        if reads_type == "fasta" or reads_type == "fasta_gzip":
            sequence = ""
            for line in reads_file:
                if line.startswith('>'):
                    if sequence != "":
                        if len(sequence) > 2000:
                            if write_left:
                                prefix = sequence[:span]
                                print(">" + read_name, file=left_file)
                                print(prefix, file=left_file)
                            if write_right:
                                suffix = sequence[-span:]
                                print(">" + read_name, file=right_file)
                                print(suffix, file=right_file)
                        if write_full:
                            print(">" + read_name, file=full_file)
                            print(sequence, file=full_file)
                    read_name = line.strip()[1:]
                    sequence = ""
                else:
                    sequence += line.strip()
            if sequence != "":
                if len(sequence) > 2000:
                    if write_left:
                        prefix = sequence[:span]
                        print(">" + read_name, file=left_file)
                        print(prefix, file=left_file)
                    if write_right:
                        suffix = sequence[-span:]
                        print(">" + read_name, file=right_file)
                        print(suffix, file=right_file)
                if write_full:
                    print(">" + read_name, file=full_file)
                    print(sequence, file=full_file)
            reads_file.close()
        elif reads_type == "fastq" or reads_type == "fastq_gzip": 
            sequence_line_is_next = False
            for line in reads_file:
                if line.startswith('@'):
                    read_name = line.strip()[1:]
                    sequence_line_is_next = True
                elif sequence_line_is_next:
                    sequence_line_is_next = False
                    sequence = line.strip()
                    if len(sequence) > 2000:
                        if write_left:
                            prefix = sequence[:span]
                            print(">" + read_name, file=left_file)
                            print(prefix, file=left_file)
                        if write_right:
                            suffix = sequence[-span:]
                            print(">" + read_name, file=right_file)
                            print(suffix, file=right_file)
                    if write_full:
                        print(">" + read_name, file=full_file)
                        print(sequence, file=full_file)
            reads_file.close()
    
    if write_left:
        left_file.close()
    if write_right:
        right_file.close()
    if write_full:
        full_file.close()

    logging.info("Read tail and full read files written")
    return full_reads_path


def run_alignments(working_dir, genome, reads_path, cores):
    """Align full reads and read tails with NGM-LR and BWA MEM, respectively."""
    reads_file_prefix = os.path.splitext(os.path.basename(reads_path))[0]
    left_fa = "{0}/{1}_left.fa".format(working_dir, reads_file_prefix)
    left_aln = "{0}/{1}_left_aln.querysorted.bam".format(working_dir, reads_file_prefix)
    right_fa = "{0}/{1}_right.fa".format(working_dir, reads_file_prefix)
    right_aln = "{0}/{1}_right_aln.querysorted.bam".format(working_dir, reads_file_prefix)
    full_aln = "{0}/{1}_aln.querysorted.bam".format(working_dir, reads_file_prefix)

    if not os.path.exists(genome + ".bwt") and (not os.path.exists(left_aln) or not os.path.exists(right_aln)):
        if call(['/scratch/ngsvin/bin/bwa.kit/bwa', 'index', genome]) != 0:
            logging.error("Calling bwa index on genome failed")

    if not os.path.exists(left_aln):
        bwa = Popen(['/scratch/ngsvin/bin/bwa.kit/bwa',
                     'mem', '-x', 'pacbio', '-t', str(cores), genome, left_fa], stdout=PIPE)
        view = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'view', '-b', '-@', str(cores)], stdin=bwa.stdout, stdout=PIPE)
        sort = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'sort', '-@', str(cores), '-n', '-o', left_aln], stdin=view.stdout)
        sort.wait()
    else:
        logging.warning("Alignment for left sequences exists. Skip")

    if not os.path.exists(right_aln):
        bwa = Popen(['/scratch/ngsvin/bin/bwa.kit/bwa',
                     'mem', '-x', 'pacbio', '-t', str(cores), genome, right_fa], stdout=PIPE)
        view = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'view', '-b', '-@', str(cores)], stdin=bwa.stdout, stdout=PIPE)
        sort = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'sort', '-@', str(cores), '-n', '-o', right_aln], stdin=view.stdout)
        sort.wait()
    else:
        logging.warning("Alignment for right sequences exists. Skip")

    # Align full reads with NGM-LR
    if not os.path.exists(full_aln):
        ngmlr = Popen(['/home/heller_d/bin/miniconda2/bin/ngmlr',
                       '-t', str(cores), '-r', genome, '-q', os.path.realpath(reads_path), ], stdout=PIPE)
        view = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'view', '-b', '-@', str(cores)], stdin=ngmlr.stdout, stdout=PIPE)
        sort = Popen(['/scratch/ngsvin/bin/samtools/samtools-1.3.1/samtools',
                      'sort', '-n', '-@', str(cores), '-o', full_aln],
                     stdin=view.stdout)
        sort.wait()
    else:
        logging.warning("Alignment for full sequences exists. Skip")

    logging.info("Alignment finished")


def natural_representation(qname): 
    """Splits a read name into a tuple of strings and numbers. This facilitates the sort order applied by samtools -n
       See https://www.biostars.org/p/102735/"""
    return [int(s) if s.isdigit() else s for s in re.split(r'(\d+)', qname)]


def bam_iterator(bam):
    """Returns an iterator for the given SAM/BAM file (must be query-sorted). 
    In each call, the alignments of a single read are yielded as a 4-tuple: (read_name, list of primary pysam.AlignedSegment, list of supplementary pysam.AlignedSegment, list of secondary pysam.AlignedSegment)."""
    alignments = bam.fetch(until_eof=True)
    current_aln = alignments.next()
    current_read_name = current_aln.query_name
    current_prim = []
    current_suppl = []
    current_sec = []
    if current_aln.is_secondary:
        current_sec.append(current_aln)
    elif current_aln.is_supplementary:
        current_suppl.append(current_aln)
    else:
        current_prim.append(current_aln)
    while True:
        try:
            next_aln = alignments.next()
            next_read_name = next_aln.query_name
            if next_read_name != current_read_name:
                yield (current_read_name, current_prim, current_suppl, current_sec)
                current_read_name = next_read_name
                current_prim = []
                current_suppl = []
                current_sec = []
            if next_aln.is_secondary:
                current_sec.append(next_aln)
            elif next_aln.is_supplementary:
                current_suppl.append(next_aln)
            else:
                current_prim.append(next_aln)
        except StopIteration:
            break
    yield (current_read_name, current_prim, current_suppl, current_sec)


def analyze_read_tails(working_dir, genome, reads_path, reads_type, parameters):
    reads_file_prefix = os.path.splitext(os.path.basename(reads_path))[0]
    left_aln = "{0}/{1}_left_aln.querysorted.bam".format(working_dir, reads_file_prefix)
    right_aln = "{0}/{1}_right_aln.querysorted.bam".format(working_dir, reads_file_prefix)

    left_bam = pysam.AlignmentFile(left_aln)
    right_bam = pysam.AlignmentFile(right_aln)
    left_it = bam_iterator(left_bam)
    right_it = bam_iterator(right_bam)

    if reads_type == "fasta" or reads_type == "fasta_gzip" or reads_type == "fastq_gzip":
        reads = SeqIO.index_db(reads_path + ".idx", reads_path, "fasta")
    elif reads_type == "fastq":
        reads = SeqIO.index_db(reads_path + ".idx", reads_path, "fastq")
    reference =SeqIO.index_db(genome + ".idx", genome, "fasta")
    logging.info("Indexing reads and reference finished")

    sv_evidences = []

    left_iterator_object = left_it.next()
    while True:
        try:
            right_iterator_object = right_it.next()             
            while natural_representation(left_iterator_object[0]) < natural_representation(right_iterator_object[0]):
                left_iterator_object = left_it.next()
            if left_iterator_object[0] == right_iterator_object[0]:
                if int(left_iterator_object[0].split("_")[1]) % 1000 == 0:
                    logging.info("Processed read {0}".format(left_iterator_object[0].split("_")[1]))
                sv_evidences.extend(analyze_pair_of_read_tails(left_iterator_object, right_iterator_object, left_bam, right_bam, reads, reference, parameters))
                left_iterator_object = left_it.next()
        except StopIteration:
            break
        except KeyboardInterrupt:
            logging.warning('Execution interrupted by user. Stop detection and continue with next step..')
            break
    return sv_evidences


def analyze_indel(bam_path, parameters):
    full_bam = pysam.AlignmentFile(bam_path)
    full_it = bam_iterator(full_bam)

    sv_evidences = []
    read_nr = 0

    while True:
        try:
            full_iterator_object = full_it.next()
            read_nr += 1
            if read_nr % 10000 == 0:
                logging.info("Processed read {0}".format(read_nr))
            sv_evidences.extend(analyze_full_read_indel(full_iterator_object, full_bam, parameters))
        except StopIteration:
            break
        except KeyboardInterrupt:
            logging.warning('Execution interrupted by user. Stop detection and continue with next step..')
            break
    return sv_evidences


def analyze_segments(bam_path, parameters):
    full_bam = pysam.AlignmentFile(bam_path)
    full_it = bam_iterator(full_bam)

    sv_evidences = []
    read_nr = 0

    while True:
        try:
            full_iterator_object = full_it.next()
            read_nr += 1
            if read_nr % 10000 == 0:
                logging.info("Processed read {0}".format(read_nr))
            sv_evidences.extend(analyze_full_read_segments(full_iterator_object, full_bam, parameters))
        except StopIteration:
            break
        except KeyboardInterrupt:
            logging.warning('Execution interrupted by user. Stop detection and continue with next step..')
            break
    return sv_evidences


def analyze_specific_read(working_dir, genome, reads_path, reads_type, parameters, read_name):
    reads_file_prefix = os.path.splitext(os.path.basename(reads_path))[0]
    left_aln = "{0}/{1}_left_aln.querysorted.bam".format(working_dir, reads_file_prefix)
    right_aln = "{0}/{1}_right_aln.querysorted.bam".format(working_dir, reads_file_prefix)
    left_bam = pysam.AlignmentFile(left_aln)
    right_bam = pysam.AlignmentFile(right_aln)
    left_it = bam_iterator(left_bam)
    right_it = bam_iterator(right_bam)

    if reads_type == "fasta" or reads_type == "fasta_gzip":
        reads = SeqIO.index_db(reads_path + ".idx", reads_path, "fasta")
    elif reads_type == "fastq" or reads_type == "fastq_gzip":
        reads = SeqIO.index_db(reads_path + ".idx", reads_path, "fastq")
    reference = SeqIO.index_db(genome + ".idx", genome, "fasta")
    logging.info("Indexing reads and reference finished")

    left_iterator_object = left_it.next()
    while left_iterator_object[0] != read_name:
        try:
            left_iterator_object = left_it.next()
        except StopIteration:
            return
    right_iterator_object = right_it.next()
    while right_iterator_object[0] != read_name:
        try:
            right_iterator_object = right_it.next()
        except StopIteration:
            return

    analyze_pair_of_read_tails(left_iterator_object, right_iterator_object, left_bam, right_bam, reads, reference, parameters)



def read_file_list(path):
    file_list = open(path, "r")
    for line in file_list:
        yield line.strip()
    file_list.close()


def main():
    # Fetch command-line options and set parameters accordingly
    options = parse_arguments()
    parameters = callPacParams()
    parameters.set_with_options(options)

    # Set up logging
    logFormatter = logging.Formatter("%(asctime)s [%(levelname)-7.7s]  %(message)s")
    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.INFO)

    fileHandler = logging.FileHandler("{0}/log_{1}.log".format(options.working_dir, strftime("%y%m%d_%H%M%S", localtime())), mode="w")
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    logging.info("****************** Start callPac, version {0} ******************".format(__version__))
    logging.info("CMD: python {0}".format(" ".join(sys.argv)))

    # Search for SV evidences
    if options.sub == 'load':
        logging.info("Started mode 'load'")
        logging.info("Load existing sv_evidences.obj with SV evidences..")
        evidences_file = open(options.working_dir + '/sv_evidences.obj', 'r')
        sv_evidences = pickle.load(evidences_file)
        evidences_file.close()
    elif options.sub == 'reads':
        logging.info("Started mode 'reads'")
        logging.info("Loading reads file..")
        reads_type = guess_file_type(options.reads_file)
        if reads_type == "unknown":
            return
        elif reads_type == "list":
            # List of read files
            sv_evidences = []
            for file_path in read_file_list(options.reads_file):
                reads_type = guess_file_type(file_path)
                full_reads_path = create_tail_files(options.working_dir, file_path, reads_type, parameters.tail_span)
                run_alignments(options.working_dir, options.genome, full_reads_path, options.cores)
                if options.read_name != "all":
                    analyze_specific_read(options.working_dir, options.genome, full_reads_path, reads_type, parameters, options.read_name)
                    continue

                reads_file_prefix = os.path.splitext(os.path.basename(full_reads_path))[0]
                full_aln = "{0}/{1}_aln.querysorted.bam".format(options.working_dir, reads_file_prefix)
                if not options.skip_kmer:
                    logging.info("Analyzing read tails..")
                    sv_evidences.extend(analyze_read_tails(options.working_dir, options.genome, full_reads_path, reads_type, parameters))
                if not options.skip_indel:
                    logging.info("Analyzing indels..")
                    sv_evidences.extend(analyze_indel(full_aln, parameters))
                if not options.skip_segment:
                    logging.info("Analyzing read segments..")
                    sv_evidences.extend(analyze_segments(full_aln, parameters))
            evidences_file = open(options.working_dir + '/sv_evidences.obj', 'w')
            logging.info("Storing collected evidences into sv_evidences.obj..")
            pickle.dump(sv_evidences, evidences_file) 
            evidences_file.close()
        else:
            # Single read file
            full_reads_path = create_tail_files(options.working_dir, options.reads_file, reads_type, parameters.tail_span)
            run_alignments(options.working_dir, options.genome, full_reads_path, options.cores)
            if options.read_name != "all":
                analyze_specific_read(options.working_dir, options.genome, full_reads_path, reads_type, parameters, options.read_name)
                return
            sv_evidences = []
            reads_file_prefix = os.path.splitext(os.path.basename(full_reads_path))[0]
            full_aln = "{0}/{1}_aln.querysorted.bam".format(options.working_dir, reads_file_prefix)
            if not options.skip_kmer:
                sv_evidences.extend(analyze_read_tails(options.working_dir, options.genome, full_reads_path, reads_type, parameters))
            if not options.skip_indel:
                sv_evidences.extend(analyze_indel(full_aln, parameters))
            if not options.skip_segment:
                sv_evidences.extend(analyze_segments(full_aln, parameters))
            evidences_file = open(options.working_dir + '/sv_evidences.obj', 'w')
            logging.info("Storing collected evidences into sv_evidences.obj..")
            pickle.dump(sv_evidences, evidences_file) 
            evidences_file.close()
    elif options.sub == 'alignment':
        logging.info("Started mode 'alignment'")
        logging.info("Loading alignment file..")
        sv_evidences = []
        if not options.skip_indel:
            logging.info("Analyzing indels..")
            sv_evidences.extend(analyze_indel(options.bam_file.name, parameters))
        if not options.skip_segment:
            logging.info("Analyzing read segments..")
            sv_evidences.extend(analyze_segments(options.bam_file.name, parameters))
        evidences_file = open(options.working_dir + '/sv_evidences.obj', 'w')
        logging.info("Storing collected evidences into sv_evidences.obj..")
        pickle.dump(sv_evidences, evidences_file)
        evidences_file.close()

    # Post-process SV evidences
    logging.info("Starting post-processing..")
    try:
        post_processing(sv_evidences, options.working_dir, options.genome, parameters)
    except AttributeError:
        post_processing(sv_evidences, options.working_dir, "unknown", parameters)

if __name__ == "__main__":
    sys.exit(main())
