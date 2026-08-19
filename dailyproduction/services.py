from datetime import datetime, time

from dailyproduction.models import Shift, SHIFT_HOURS, SHIFT_LABELS


def get_current_shift():
    """Get the current shift based on the wall-clock time."""
    now = datetime.now().time()
    if time(6, 0) <= now < time(14, 0):
        return Shift.SHIFT_1
    elif time(14, 0) <= now < time(22, 0):
        return Shift.SHIFT_2
    return Shift.SHIFT_3


def get_shift_hours(shift):
    """List of hours (0-23) belonging to a given shift."""
    return list(SHIFT_HOURS.get(shift, []))


def format_machine_name(machine_enum):
    return machine_enum.value.replace("_", " ").title()


def format_shift_name(shift_enum):
    return SHIFT_LABELS.get(shift_enum, "Unknown Shift")


def calculate_shift_efficiency(shift_data):
    """Basic efficiency metrics for a set of ProductionEntry rows."""
    if not shift_data:
        return {"efficiency": 0, "avg_hourly": 0, "total_cores": 0}

    total_cores = sum(entry.cores_produced for entry in shift_data)
    total_hours = len(shift_data)
    avg_hourly = total_cores / total_hours if total_hours > 0 else 0

    target_per_hour = 100
    efficiency = (avg_hourly / target_per_hour) * 100 if target_per_hour > 0 else 0

    return {
        "efficiency": round(efficiency, 1),
        "avg_hourly": round(avg_hourly, 1),
        "total_cores": total_cores,
    }
