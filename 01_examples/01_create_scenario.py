import sys
sys.path.append("..")  # Add the parent directory to the Python path for execution outside of an IDE
sys.path.append("./")  # Add the current directory to the Python path for execution in VSCode
from hamlet import Creator

# Path to the scenario folder (relative or absolute)
path = "../02_config/example_single_market"

# Create the creator object
sim = Creator(path=path)

# Create the scenario
sim.new_scenario_from_configs()

# Alternative methods to create the scenario:
# sim.new_scenario_from_grids()
# sim.new_scenario_from_files()
