import time
import os
import logging

_log = logging.getLogger(__name__)


def sortRecordAttributes(attributes, processor, keep_all_attributes=True):
    processor_attributes = processor["attributes"]

    ## match record attribute to each processor attribute
    sorted_attributes = []
    processor_attributes_list = []
    for each in processor_attributes:
        attribute_name = each["name"]
        processor_attributes_list.append(attribute_name)

        found_ = [item for item in attributes if item["key"] == attribute_name]
        for attribute in found_:
            if attribute is None:
                _log.info(f"{attribute_name} is None")
            else:
                sorted_attributes.append(attribute)

    if keep_all_attributes:
        for attr in attributes:
            attribute_name = attr["key"]
            if attribute_name not in processor_attributes_list:
                _log.debug(
                    f"{attribute_name} was not in processor's attributes. adding this to the end of the sorted attributes list"
                )
                sorted_attributes.append(attr)

    return sorted_attributes


def imageIsValid(image):
    ## some broken records have letters saved where image names should be
    ## for now, just ensure that the name is at least 3 characters long
    try:
        if len(image) > 2:
            return True
        else:
            return False
    except Exception as e:
        _log.error(f"unable to check validity of image: {e}")
        return False


def deleteFiles(filepaths, sleep_time=5):
    _log.info(f"deleting files: {filepaths} in {sleep_time} seconds")
    time.sleep(sleep_time)
    for filepath in filepaths:
        if os.path.isfile(filepath):
            os.remove(filepath)
            _log.info(f"deleted {filepath}")
