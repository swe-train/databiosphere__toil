from argparse import ArgumentParser

from configargparse import SUPPRESS


def add_wdl_options(parser: ArgumentParser, suppress: bool = True) -> None:
    """
    Add WDL options to a parser. This only adds nonpositional WDL arguments
    :param parser: Parser to add options to
    :param suppress: Suppress help output
    :return: None
    """
    suppress_help = SUPPRESS if suppress else None
    # include arg names without a wdl specifier if suppress is False
    # this is to avoid possible duplicate options in custom toil scripts, ex outputFile can be a common argument name
    output_dialect_arguments = ["--wdlOutputDialect"] + (["--outputDialect"] if not suppress else [])
    parser.add_argument(*output_dialect_arguments, dest="output_dialect", type=str, default='cromwell',
                        choices=['cromwell', 'miniwdl'],
                        help=suppress_help or ("JSON output format dialect. 'cromwell' just returns the workflow's "
                                               "output values as JSON, while 'miniwdl' nests that under an 'outputs' "
                                               "key, and includes a 'dir' key where files are written."))
    output_directory_arguments = ["--wdlOutputDirectory"] + (["--outputDirectory", "-o"] if not suppress else [])
    parser.add_argument(*output_directory_arguments, dest="output_directory", type=str, default=None,
                        help=suppress_help or (
                            "Directory or URI prefix to save output files at. By default a new directory is created "
                            "in the current directory."))
    output_file_arguments = ["--wdlOutputFile"] + (["--outputFile", "-m"] if not suppress else [])
    parser.add_argument(*output_file_arguments, dest="output_file", type=str, default=None,
                        help=suppress_help or "File or URI to save output JSON to.")
    reference_inputs_arguments = ["--wdlReferenceInputs"] + (["--referenceInputs"] if not suppress else [])
    parser.add_argument(reference_inputs_arguments, dest="reference_inputs", type="bool", default=False,  # type: ignore
                        help="Pass input files by URL")
