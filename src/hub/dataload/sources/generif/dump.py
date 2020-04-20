import os
import os.path
import sys
import time
from datetime import datetime

import biothings, config
biothings.config_for_app(config)

from config import DATA_ARCHIVE_ROOT, logger as logging
from biothings.hub.dataload.dumper import FTPDumper


class GenerifDumper(FTPDumper):

    SRC_NAME = "generif"
    SRC_ROOT_FOLDER = os.path.join(DATA_ARCHIVE_ROOT, SRC_NAME)
    FTP_HOST = 'ftp.ncbi.nih.gov'
    CWD_DIR = '/gene/GeneRIF'

    SCHEDULE = "0 7 * * *"

    def get_newest_info(self):
        res = self.client.sendcmd("MDTM generifs_basic.gz") # pick one, assuming all other on the same data
        code, remote_lastmodified = res.split()
        self.release = datetime.strptime(remote_lastmodified, '%Y%m%d%H%M%S').strftime("%Y%m%d")

    def new_release_available(self):
        current_release = self.src_doc.get("download",{}).get("release")
        if not current_release or self.release > current_release:
            self.logger.info("New release '%s' found" % self.release)
            return True
        else:
            self.logger.debug("No new release found")
            return False

    def create_todump_list(self, force=False):
        self.get_newest_info()
        for fn in ["generifs_basic.gz"]:
            local_file = os.path.join(self.new_data_folder,fn)
            if force or not os.path.exists(local_file) or self.remote_is_better(fn,local_file) or self.new_release_available():
                self.to_dump.append({"remote": fn, "local":local_file})

