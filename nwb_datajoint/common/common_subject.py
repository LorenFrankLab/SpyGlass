# Test of automatic datajoint schema generation from NWB file
import datajoint as dj

schema = dj.schema("common_subject")


@schema
class Subject(dj.Manual):
    definition = """
    subject_id: varchar(80)
    ---
    age : varchar(80)
    description : varchar(80)
    genotype : varchar(80)
    sex : enum('M', 'F', 'U')
    species : varchar(80)
    """

    def __init__(self, *args):
        super().__init__(*args)  # call the base implementation

    def insert_from_nwbfile(self, nwbf):
        # get the subject information and create a dictionary from it
        sub = nwbf.subject
        subject_dict = dict()
        subject_dict['subject_id'] = sub.subject_id
        if sub.age is None:
            subject_dict['age'] = 'unknown'
        subject_dict['description'] = sub.description
        subject_dict['genotype'] = sub.genotype
        if (sub.sex == 'Male'):
            sex = 'M'
        elif (sub.sex == 'Female'):
            sex = 'F'
        else:
            sex = 'U'
        subject_dict['sex'] = sex
        subject_dict['species'] = sub.species
        self.insert1(subject_dict, skip_duplicates=True)
