"""Module to query ES indexes"""

#http://www.elasticsearch.org/guide/reference/query-dsl/custom-filters-score-query.html
#http://www.elasticsearch.org/guide/reference/query-dsl/custom-score-query.html
#http://www.elasticsearch.org/guide/reference/query-dsl/custom-boost-factor-query.html
#http://www.elasticsearch.org/guide/reference/query-dsl/boosting-query.html
import sys

import json
import re
import time
import copy
import requests
import logging

from config import (ES_INDEX_NAME_TIER1, ES_INDEX_NAME,
                    ES_DOC_TYPE, SOURCE_TRANSLATORS)
from biothings.utils.common import (ask, is_int, is_str, is_seq, timesofar, dotdict)
from biothings.utils.dotfield import parse_dot_fields, compose_dot_fields_by_fields as compose_dot_fields
from biothings.www.api.es import ESQuery, QueryError, ESQueryBuilder, parse_facets_option
from elasticsearch import Elasticsearch
from .userfilters import UserFilters




GENOME_ASSEMBLY = {
    "human": "hg38",
    "mouse": "mm10",
    "rat": "rn4",
    "fruitfly": "dm3",
    "nematode": "ce10",
    "zebrafish": "zv9",
    "frog": "xenTro3",
    "pig": "susScr2"
}

TAXONOMY = {
    "human": 9606,
    "mouse": 10090,
    "rat": 10116,
    "fruitfly": 7227,
    "nematode": 6239,
    "zebrafish": 7955,
    "thale-cress": 3702,
    "frog": 8364,
    "pig": 9823
}


def safe_genome_pos(s):
    '''
       safe_genome_pos(1000) = 1000
       safe_genome_pos('1000') = 1000
       safe_genome_pos('10,000') = 100000
    '''
    if isinstance(s, int):
        return s
    elif isinstance(s, str):
        return int(s.replace(',', ''))
    else:
        raise ValueError('invalid type "%s" for "save_genome_pos"' % type(s))



class ESQuery(ESQuery):
    def __init__(self):
        super( ESQuery, self ).__init__()
        self._default_fields = ['name', 'symbol', 'taxid', 'entrezgene']
        self._default_species = [9606, 10090, 10116] # human, mouse, rat
        self._tier_1_species = set(TAXONOMY.values())

    def _search(self, q, species='all', scroll_options={},**kwargs):
        self._set_index(species)
        # body = '{"query" : {"term" : { "_all" : ' + q + ' }}}'
        #logging.error("in search q: %s -- index: %s -- doc_type: %s" % (json.dumps(q),self._index,self._doc_type))

        res = self._es.search(index=self._index, doc_type=self._doc_type,
                               body=q, **scroll_options)
        #logging.error("in search: %s" % res)
        self._index = ES_INDEX_NAME # reset self._index
        return res

    def _msearch(self, **kwargs):
        self._set_index(kwargs.get('species', 'all'))
        logging.debug("_msearch: %s" % kwargs['body'])
        res = super(ESQuery, self)._msearch(**kwargs)
        self._index = ES_INDEX_NAME     # reset self._index
        return res

    def _set_index(self, species):
        '''set proper index for given species parameter.'''
        if species == 'all' or len(set(species)-self._tier_1_species) > 0:
            self._index = ES_INDEX_NAME
        else:
            self._index = ES_INDEX_NAME_TIER1

    def _get_query_builder(self,**kwargs):
        return ESQueryBuilder(**kwargs) 

    def _build_query(self, q, kwargs):
        # can override this function if more query types are to be added
        esqb = self._get_query_builder(**kwargs)
        return esqb.query(q)

    def _cleaned_species(self, species, default_to_none=False):
        '''return a cleaned species parameter.
           should be either "all" or a list of taxids/species_names, or a single taxid/species_name.
           returned species is always a list of taxids (even when only one species)
        '''
        if species is None:
            #set to default_species
            return None if default_to_none else self._default_species
        if isinstance(species, int):
            return [species]

        if is_str(species):
            if species.lower() == 'all':
                #if self.species == 'all': do not apply species filter, all species is included.
                return species.lower()
            else:
                species = [s.strip().lower() for s in species.split(',')]

        if not is_seq(species):
            raise ValueError('"species" parameter must be a string, integer or a list/tuple, not "{}".'.format(type(species)))

        _species = []
        for s in species:
            if is_int(s):
                _species.append(int(s))
            elif s in TAXONOMY:
                _species.append(TAXONOMY[s])
        return _species


    def _get_options(self,options,kwargs):
        #this species parameter is added to the query, thus will change facet counts.
        kwargs['species'] = self._cleaned_species(kwargs.get('species', None))
        include_tax_tree = kwargs.pop('include_tax_tree', False)
        if include_tax_tree:
            headers = {'content-type': 'application/x-www-form-urlencoded',
                      'user-agent': "Python-requests_mygene.info/%s (gzip)" % requests.__version__}
            #TODO: URL as config param
            res = requests.post('http://s.biothings.io/v1/species?ids=' + 
                                ','.join(['{}'.format(sid) for sid in kwargs['species']]) +
                                '&expand_species=true', headers=headers)
            if res.status_code == requests.codes.ok:
                kwargs['species'] = res.json()

        #this parameter is to add species filter without changing facet counts.
        kwargs['species_facet_filter'] = self._cleaned_species(kwargs.get('species_facet_filter', None),
                                                               default_to_none=True)
        options.kwargs = kwargs
        return options


    def metadata(self, raw=False):
        '''return metadata about the index.'''
        mapping = self._es.indices.get_mapping(self._index, self._doc_type)
        if raw:
            return mapping

        def get_fields(properties):
            for k, v in list(properties.items()):
                if 'properties' in v:
                    for f in get_fields(v['properties']):
                        yield f
                else:
                    if v.get('index', None) == 'no':
                        continue
                    f = v.get('index_name', k)
                    yield f
        mapping = list(mapping.values())[0]['mappings']
        #field_set = set(get_fields(mapping[self._doc_type]['properties']))
        metadata = {
            #'available_fields': sorted(field_set)
            # TODO: http://mygene.info as config
            'available_fields': 'http://mygene.info/metadata/fields'
        }
        if '_meta' in mapping[self._doc_type]:
            metadata.update(mapping[self._doc_type]['_meta'])
        metadata['genome_assembly'] = GENOME_ASSEMBLY
        metadata['taxonomy'] = TAXONOMY
        return metadata


class ESQueryBuilder(ESQueryBuilder):
    def __init__(self, **query_options):
        """You can pass these options:
            fields     default ['name', 'symbol', 'taxid', 'entrezgene']
            from       default 0
            size       default 10
            sort       e.g. sort='entrezgene,-symbol'
            explain    true or false
            facets     a field or a list of fields, default None

            species
            species_facet_filter
            entrezonly  default false
            ensemblonly default false
            userfilter  optional, provide the name of a saved user filter (in "userfilters" index)
            exists      optional, passing field, comma-separated fields, returned
                                  genes must have given field(s).
            missing     optional, passing field, comma-separated fields, returned
                                  genes must have NO given field(s).

        """
        super( ESQueryBuilder, self ).__init__()
        self._query_options = query_options
        self.species = self._query_options.pop('species', 'all')   # species should be either 'all' or a list of taxids.
        self.species_facet_filter = self._query_options.pop('species_facet_filter', None)
        self.entrezonly = self._query_options.pop('entrezonly', False)
        self.ensemblonly = self._query_options.pop('ensemblonly', False)
        # userfilter
        userfilter = self._query_options.pop('userfilter', None)
        self.userfilter = userfilter.split(',') if userfilter else None
        # exist filter
        existsfilter = self._query_options.pop('exists', None)
        self.existsfilter = existsfilter.split(',') if existsfilter else None
        # missing filter
        missingfilter = self._query_options.pop('missing', None)
        self.missingfilter = missingfilter.split(',') if missingfilter else None

        parse_facets_option(self._query_options)
        self._allowed_options = ['fields', '_source', 'start', 'from', 'size',
                                 'sort', 'explain', 'version', 'aggs','dotfield']
        for key in set(self._query_options) - set(self._allowed_options):
            del self._query_options[key]
        # convert "fields" option to "_source"
        # use "_source" instead of "fields" for ES v1.x and up
        if 'fields' in self._query_options and self._query_options['fields'] is not None:
            self._query_options['_source'] = self._query_options['fields']
            del self._query_options['fields']

        # this is a fake query to make sure to return empty hits
        self._nohits_query = {
            "match": {
                'non_exist_field': ''
            }
        }

    def _translate_datasource(self, q):
        for src in SOURCE_TRANSLATORS.keys():
            q = re.sub(src, SOURCE_TRANSLATORS[src], q)
        logging.debug(q)
        return q
        pass


    def _parse_interval_query(self, query):
        '''Check if the input query string matches interval search regex,
           if yes, return a dictionary with three key-value pairs:
              chr
              gstart
              gend
            , otherwise, return None.
        '''
        pattern = r'chr(?P<chr>\w+):(?P<gstart>[0-9,]+)-(?P<gend>[0-9,]+)'
        if query:
            mat = re.search(pattern, query)
            if mat:
                d = mat.groupdict()
                if query.startswith('hg19.'):
                    # support hg19 for human (default is hg38)
                    d['assembly'] = 'hg19'
                if query.startswith('mm9.'):
                    # support mm9 for mouse (default is mm10)
                    d['assembly'] = 'mm9'

                return d


    def dis_max_query(self, q):
        #remove '"' and '\' from q, they will break json decoder.
        q = q.replace('"', '').replace('\\', '')
        _query = {
            "dis_max": {
                "tie_breaker": 0,
                "boost": 1,
                "queries": [
                    {
                        "function_score": {
                            "query": {
                                "match": {
                                    "symbol": {
                                        "query": "%(q)s",
                                        "analyzer": "whitespace_lowercase"
                                    }
                                },
                            },
                            "weight": 5
                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                #This makes phrase match of "cyclin-dependent kinase 2" appears first
                                "match_phrase": {"name": "%(q)s"},
                            },
                            "weight": 4

                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                "match": {
                                    "name": {
                                        "query": "%(q)s",
                                        "operator": "and",
                                        "analyzer": "whitespace_lowercase"
                                    }
                                },
                            },
                            "weight": 3
                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                "match": {
                                    "unigene": {
                                        "query": "%(q)s",
                                        "analyzer": "string_lowercase"
                                    }
                                }
                            },
                            "weight": 1.1
                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                "match": {
                                    "go": {
                                        "query": "%(q)s",
                                        "analyzer": "string_lowercase"
                                    }
                                }
                            },
                            "weight": 1.1
                        }
                    },
                    # {
                    # "custom_boost_factor": {
                    #     "query" : {
                    #         "match" : { "_all" : {
                    #                         "query": "%(q)s",
                    #                         "analyzer": "whitespace_lowercase"
                    #             }
                    #         },
                    #     },
                    #     "boost_factor": 1
                    # }
                    # },
                    {
                        "function_score": {
                            "query": {
                                "query_string": {
                                    "query": "%(q)s",
                                    "default_operator": "AND",
                                    "auto_generate_phrase_queries": True
                                },
                            },
                            "weight": 1
                        }
                    },

                ]
            }
        }
        _query = json.dumps(_query)
        _query = json.loads(_query % {'q': q})

        if is_int(q):
            _query['dis_max']['queries'] = []
            _query['dis_max']['queries'].insert(
                0,
                {
                    "function_score": {
                        "query": {
                            "term": {"entrezgene": int(q)},
                        },
                        "weight": 8
                    }
                }
            )

        return _query

    def wildcard_query(self, q):
        '''q should contains either * or ?, but not the first character.'''
        _query = {
            "dis_max": {
                "tie_breaker": 0,
                "boost": 1,
                "queries": [
                    {
                        "function_score": {
                            "query": {
                                "wildcard": {
                                    "symbol": {
                                        "value": "%(q)s",
                                        # "weight": 5.0,
                                    }
                                },
                            },
                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                "wildcard": {
                                    "name": {
                                        "value": "%(q)s",
                                        # "weight": 1.1,
                                    }
                                },
                            }
                        }
                    },
                    {
                        "function_score": {
                            "query": {
                                "wildcard": {
                                    "summary": {
                                        "value": "%(q)s",
                                        # "weight": 0.5,
                                    }
                                },
                            }
                        }
                    },

                ]
            }
        }
        _query = json.dumps(_query)
        try:
            _query = json.loads(_query % {'q': q.lower()})
        except ValueError:
            raise QueryError("invalid query term.")

        return _query

    def get_query_filters(self):
        '''filters added here will be applied in a filtered query,
           thus will affect the facet counts.
        '''
        filters = []
        #species filter
        if self.species and self.species != 'all':
            if len(self.species) == 1:
                filters.append({
                    "term": {"taxid": self.species[0]}
                })
            else:
                filters.append({
                    "terms": {"taxid": self.species}
                })
        if self.entrezonly:
            filters.append({
                "exists": {"field": "entrezgene"}
            })
        if self.ensemblonly:
            filters.append({
                "exists": {"field": "ensemblgene"}
            })

        if self.userfilter:
            _uf = UserFilters()
            for _fname in self.userfilter:
                _filter = _uf.get(_fname)
                if _filter:
                    filters.append(_filter['filter'])

        if self.existsfilter:
            for _filter in self.existsfilter:
                filters.append({
                    "exists": {"field": _filter}
                })
        if self.missingfilter:
            for _filter in self.missingfilter:
                filters.append({
                    "missing": {"field": _filter}
                })

        if filters:
            if len(filters) == 1:
                filters = filters[0]
            else:
                #concatenate multiple filters with "and" filter
                filters = {"and": filters}

        return filters

    def add_facet_filters(self, _query):
        """To add filters (e.g. taxid) to restrict returned hits,
            but does not change the scope for facet counts.
        """
        filters = []
        #species_facet_filter
        if self.species_facet_filter:
            if len(self.species) == 1:
                filters.append({
                    "term": {"taxid": self.species_facet_filter[0]}
                })
            else:
                filters.append({
                    "terms": {"taxid": self.species_facet_filter}
                })
        if filters:
            if len(filters) == 1:
                filters = filters[0]
            else:
                #concatenate multiple filters with "and" filter
                filters = {"and": filters}

            #this will not change facet counts
            _query["filter"] = filters

        return _query

    def add_species_custom_filters_score(self, _query):
        _query = {
            "function_score": {
                "query": _query,
                "functions": [
                    #downgrade "pseudogene" matches
                    {
                        "filter": {"term": {"name": "pseudogene"}},
                        "boost_factor": "0.5"
                    },
                    {
                        "filter": {"term": {"taxid": 9606}},
                        "boost_factor": "1.55"
                    },
                    {
                        "filter": {"term": {"taxid": 10090}},
                        "boost_factor": "1.3"
                    },
                    {
                        "filter": {"term": {"taxid": 10116}},
                        "boost_factor": "1.1"
                    },
                ],
                "score_mode": "first"
            }
        }
        return _query

    def query(self, q):
        '''mode:
                1    match query
                2    wildcard query
                3    raw_string query

               else  string_query (for test)
        '''

        # translate data source to provide back-compatibility for
        # some query fields running on ES2
        logging.debug("lkjlkj %s" % repr(q))
        q = self._translate_datasource(q)
        logging.debug("now lkjlkj %s" % repr(q))

        # Check if special interval query pattern exists
        interval_query = self._parse_interval_query(q)
        if interval_query:
            # should also passing a "taxid" along with interval.
            logging.debug("%s ...." % self.species)
            if self.species != 'all':
                self.species = [self.species[0]]  # TODO: where is it used ?
                _q = self.build_genomic_pos_query(**interval_query)
                return _q
            else:
                raise QueryError('genomic interval query cannot be combined ' +
                                 'with "species=all" parameter. ' +
                                 'Specify a single species.')

        else:
            _query = self.generate_query(q)

            #TODO: this is actually not used, how useful ?
            #_query = self.string_query(q)

            _query = self.add_species_custom_filters_score(_query)
            _q = {'query': _query}
            _q = self.add_facet_filters(_q)
            if self._query_options:
                _q.update(self._query_options)
            logging.debug("_q = %s" % json.dumps(_q))
            return _q

    # keepit (but similar)
    def build_id_query(self, id, scopes=None):
        id_is_int = is_int(id)
        if scopes is None:
            #by default search three fields ['entrezgene', 'ensemblgene', 'retired']
            if id_is_int:
                _query = {
                    "multi_match": {
                        "query": id,
                        "fields": ['entrezgene', 'retired']
                    }
                }
            else:
                _query = {
                    "match": {
                        "ensemblgene": {
                            "query": u"{}".format(id),
                            "operator": "and"
                        }
                    }
                }
        else:
            if is_str(scopes):
                _field = scopes
                if _field in ['entrezgene', 'retired']:
                    if id_is_int:
                        _query = {
                            "match": {
                                _field: id
                            }
                        }
                    else:
                        #raise ValueError('fields "%s" requires an integer id to query' % _field)
                        #using a fake query here to make sure return empty hits
                        _query = self._nohits_query
                else:
                    _query = {
                        "match": {
                            _field: {
                                "query": u"{}".format(id),
                                "operator": "and"
                            }
                        }
                    }
            elif is_seq(scopes):
                int_fields = []
                str_fields = copy.copy(scopes)
                if 'entrezgene' in str_fields:
                    int_fields.append('entrezgene')
                    str_fields.remove('entrezgene')
                if 'retired' in str_fields:
                    int_fields.append('retired')
                    str_fields.remove('retired')

                if id_is_int:
                    if len(int_fields) == 1:
                        _query = {
                            "match": {
                                int_fields[0]: id
                            }
                        }
                    elif len(int_fields) == 2:
                        _query = {
                            "multi_match": {
                                "query": id,
                                "fields": int_fields
                            }
                        }
                    else:
                        _query = self._nohits_query
                elif str_fields:
                    _query = {
                        "multi_match": {
                            "query": u"{}".format(id),
                            "fields": str_fields,
                            "operator": "and"
                        }
                    }
                else:
                    _query = self._nohits_query

            else:
                raise ValueError('"scopes" cannot be "%s" type' % type(scopes))

        #_query = self.add_species_filter(_query)
        _query = self.add_query_filters(_query)
        _query = self.add_species_custom_filters_score(_query)
        _q = {"query": _query}
        if self._query_options:
            _q.update(self._query_options)

        # if 'fields' in _q and _q['fields'] is not None:
        #     _q['_source'] = _q['fields']
        #     del _q['fields']
        return _q

    def build_genomic_pos_query(self, chr, gstart, gend, assembly=None):
        '''By default if assembly is None, the lastest assembly is used.
           for some species (e.g. human) we support multiple assemblies,
           exact assembly is passed as well.
        '''
        gstart = safe_genome_pos(gstart)
        gend = safe_genome_pos(gend)
        if chr.lower().startswith('chr'):
            chr = chr[3:]

        genomic_pos_field = "genomic_pos"
        if assembly:
            if assembly == 'hg19':
                genomic_pos_field = "genomic_pos_hg19"
            if assembly == 'mm9':
                genomic_pos_field = "genomic_pos_mm9"

        _query = {
            "nested": {
                "path": genomic_pos_field,
                "query": {
                    "bool": {
                        "must": [
                            {
                                "term": {genomic_pos_field + ".chr": chr.lower()}
                            },
                            {
                                "range": {genomic_pos_field + ".start": {"lte": gend}}
                            },
                            {
                                "range": {genomic_pos_field + ".end": {"gte": gstart}}
                            }
                        ]
                    }
                }
            }
        }
        # _query = {
        #     'filtered': {
        #         'query': _query,
        #         'filter' : {
        #             "term" : {"taxid" : taxid}
        #         }
        #     }
        # }
        _query = self.add_query_filters(_query)
        _q = {'query': _query}
        if self._query_options:
            _q.update(self._query_options)
        return _q


def make_test_index():

    def get_sample_gene(gene):
        qbdr = ESQueryBuilder(fields=['_source'], size=1000)
        _query = qbdr.dis_max_query(gene)
        _query = qbdr.add_species_custom_filters_score(_query)
        _q = {'query': _query}
        if qbdr.options:
            _q.update(qbdr.options)

        esq = ESQuery()
        res = esq._search(_q)
        return [h['_source'] for h in res['hits']['hits']]

    gli = get_sample_gene('CDK2') + \
        get_sample_gene('BTK') + \
        get_sample_gene('insulin')

    from utils.es import ESIndexer
    index_name = 'genedoc_2'
    index_type = 'gene_sample'
    esidxer = ESIndexer(None, None)
    conn = esidxer.conn
    try:
        esidxer.delete_index_type(index_type)
    except:
        pass
    mapping = dict(conn.get_mapping('gene', index_name)['gene'])
    print(conn.put_mapping(index_type, mapping, [index_name]))

    print("Building index...")
    cnt = 0
    for doc in gli:
        conn.index(doc, index_name, index_type, doc['_id'])
        cnt += 1
        print(cnt, ':', doc['_id'])
    print(conn.flush())
    print(conn.refresh())
    print('Done! - {} docs indexed.'.format(cnt))
