# Copyright 2003, 2004, 2005 Infrae
# Released under the BSD license (see LICENSE.txt)
from __future__ import nested_scopes
import urllib2
import base64
from urllib import urlencode
from StringIO import StringIO
from types import SliceType
from lxml import etree
import time
import codecs
import os
import gzip

from oaipmh import common, metadata, validation, error
from oaipmh.datestamp import datestamp_to_datetime, datetime_to_datestamp

WAIT_DEFAULT = 240 # four minutes
WAIT_MAX = 64

class Error(Exception):
    pass

class BaseClient(common.OAIPMH):

    def __init__(self, metadata_registry=None):
        self._metadata_registry = (
            metadata_registry or metadata.global_metadata_registry)
        self._ignore_bad_character_hack = 0
        self._fix_cairn_output_hack = True
        self._day_granularity = False
        self.extra_parameters = {}

    def updateGranularity(self):
        """Update the granularity setting dependent on that the server says.
        """
        identify = self.identify()
        granularity = identify.granularity()
        if granularity == 'YYYY-MM-DD':
            self._day_granularity = True
        elif granularity == 'YYYY-MM-DDThh:mm:ssZ':
            self._day_granularity= False
        else:
            raise Error, "Non-standard granularity on server: %s" % granularity
            
    def handleVerb(self, verb, kw):
        # validate kw first
        validation.validateArguments(verb, kw)
        # encode datetimes as datestamps
        from_ = kw.get('from_')
        if from_ is not None:
            # turn it into 'from', not 'from_' before doing actual request
            kw['from'] = datetime_to_datestamp(from_,
                                               self._day_granularity)
        if 'from_' in kw:
            # always remove it from the kw, no matter whether it be None or not
            del kw['from_']
    
        until = kw.get('until')
        if until is not None:
            kw['until'] = datetime_to_datestamp(until,
                                                self._day_granularity)
        elif 'until' in kw:
            # until is None but is explicitly in kw, remove it
            del kw['until']

        sent_kw = kw
        if 'resumptionToken' in kw:
            sent_kw = dict()
            sent_kw['resumptionToken'] = kw['resumptionToken']
        
        # now call underlying implementation
        method_name = verb + '_impl'
        return getattr(self, method_name)(
            kw, self.makeRequestErrorHandling(verb=verb, **sent_kw))    

    def getNamespaces(self):
        """Get OAI namespaces.
        """
        return {'oai': 'http://www.openarchives.org/OAI/2.0/'}

    def getMetadataRegistry(self):
        """Return the metadata registry in use.

        Do we want to allow the returning of the global registry?
        """
        return self._metadata_registry

    def ignoreBadCharacters(self, true_or_false): 	 
        """Set to ignore bad characters in UTF-8 input. 	 
        This is a hack to get around well-formedness errors of 	 
        input sources which *should* be in UTF-8 but for some reason 	 
        aren't completely. 	 
        """ 	 
        self._ignore_bad_character_hack = true_or_false 	 

    def parse(self, xml): 	 
        """Parse the XML to a lxml tree. 	 
        """
        # XXX this is only safe for UTF-8 encoded content, 	 
        # and we're basically hacking around non-wellformedness anyway,
        # but oh well
        if self._ignore_bad_character_hack: 	 
            xml = unicode(xml, 'UTF-8', 'replace') 	 
            # also get rid of character code 12 	 
            xml = xml.replace(chr(12), '?')
            xml = xml.encode('UTF-8')
        if self._fix_cairn_output_hack:
            xml = xml.replace('<span styl</dc:title>', '</dc:title>')
        return etree.XML(xml)

    # implementation of the various methods, delegated here by
    # handleVerb method

    def GetRecord_impl(self, args, tree):
        records, token = self.buildRecords(
            args['metadataPrefix'],
            self.getNamespaces(),
            self._metadata_registry,
            tree
            )
        assert token is None
        return records[0]

    def GetMetadata_impl(self, args, tree):
        return tree
    
    def Identify_impl(self, args, tree):
        namespaces = self.getNamespaces()
        evaluator = etree.XPathEvaluator(tree, namespaces=namespaces)
        identify_node = evaluator.evaluate(
            '/oai:OAI-PMH/oai:Identify')[0]
        identify_evaluator = etree.XPathEvaluator(identify_node,
                                                  namespaces=namespaces)
        e = identify_evaluator.evaluate

        repositoryName = e('string(oai:repositoryName/text())')
        baseURL = e('string(oai:baseURL/text())')
        protocolVersion = e('string(oai:protocolVersion/text())')
        adminEmails = e('oai:adminEmail/text()')
        earliestDatestamp = datestamp_to_datetime(
            e('string(oai:earliestDatestamp/text())'))
        deletedRecord = e('string(oai:deletedRecord/text())')
        granularity = e('string(oai:granularity/text())')
        compression = e('oai:compression/text()')
        # XXX description
        identify = common.Identify(
            repositoryName, baseURL, protocolVersion,
            adminEmails, earliestDatestamp,
            deletedRecord, granularity, compression)
        return identify

    def ListIdentifiers_impl(self, args, tree):
        namespaces = self.getNamespaces()
        def firstBatch():
            return self.buildIdentifiers(namespaces, tree)
        def nextBatch(token):
            tree = self.makeRequestErrorHandling(verb='ListIdentifiers',
                                                 resumptionToken=token)
            return self.buildIdentifiers(namespaces, tree)
        return ResumptionListGenerator(firstBatch, nextBatch)

    def ListMetadataFormats_impl(self, args, tree):
        namespaces = self.getNamespaces()
        evaluator = etree.XPathEvaluator(tree, 
                                         namespaces=namespaces)

        metadataFormat_nodes = evaluator.evaluate(
            '/oai:OAI-PMH/oai:ListMetadataFormats/oai:metadataFormat')
        metadataFormats = []
        for metadataFormat_node in metadataFormat_nodes:
            e = etree.XPathEvaluator(metadataFormat_node, 
                                     namespaces=namespaces).evaluate
            metadataPrefix = e('string(oai:metadataPrefix/text())')
            schema = e('string(oai:schema/text())')
            metadataNamespace = e('string(oai:metadataNamespace/text())')
            metadataFormat = (metadataPrefix, schema, metadataNamespace)
            metadataFormats.append(metadataFormat)

        return metadataFormats

    def ListRecordsFromFile(self, metadataPrefix, tree):
        namespaces = self.getNamespaces()
        metadata_registry = self._metadata_registry
        return self.buildRecords(metadataPrefix, namespaces,
                metadata_registry, tree)

    def ListRecords_impl(self, args, tree):
        namespaces = self.getNamespaces()
        metadata_registry = self._metadata_registry
        metadata_prefix = args['metadataPrefix']
        def firstBatch():
            return self.buildRecords(
                metadata_prefix, namespaces,
                metadata_registry, tree)
        def nextBatch(token):
            tree = self.makeRequestErrorHandling(
                verb='ListRecords',
                resumptionToken=token)
            return self.buildRecords(
                metadata_prefix, namespaces,
                metadata_registry, tree)
        return ResumptionListGenerator(firstBatch, nextBatch)

    def ListSets_impl(self, args, tree):
        namespaces = self.getNamespaces()
        def firstBatch():
            return self.buildSets(namespaces, tree)
        def nextBatch(token):
            tree = self.makeRequestErrorHandling(
                verb='ListSets',
                resumptionToken=token)
            return self.buildSets(namespaces, tree)
        return ResumptionListGenerator(firstBatch, nextBatch)

    # various helper methods
    
    def buildRecords(self,
                     metadata_prefix, namespaces, metadata_registry, tree):
        # first find resumption token if available
        evaluator = etree.XPathEvaluator(tree,
                                         namespaces=namespaces)
        token = evaluator.evaluate(
            'string(/oai:OAI-PMH/*/oai:resumptionToken/text())')
        if token.strip() == '':
            token = None
        record_nodes = evaluator.evaluate(
            '/oai:OAI-PMH/*/oai:record')
        result = []
        for record_node in record_nodes:
            record_evaluator = etree.XPathEvaluator(record_node, 
                                                    namespaces=namespaces)
            e = record_evaluator.evaluate
            # find header node
            header_node = e('oai:header')[0]
            # create header
            header = buildHeader(header_node, namespaces, metadata_prefix)
            format = header.format()
            # find metadata node
            metadata_list = e('oai:metadata')
            if metadata_list:
                metadata_node = metadata_list[0]
                # create metadata
                metadata = metadata_registry.readMetadata(format,
                                                          metadata_node)
            else:
                metadata = None
            # XXX TODO: about, should be third element of tuple
            result.append((header, metadata, None))
        return result, token

    def buildIdentifiers(self, namespaces, tree):
        evaluator = etree.XPathEvaluator(tree, 
                                         namespaces=namespaces)
        # first find resumption token is available
        token = evaluator.evaluate(
            'string(/oai:OAI-PMH/oai:ListIdentifiers/oai:resumptionToken/text())')
        if token.strip() == '':
            token = None    
        header_nodes = evaluator.evaluate(
                '/oai:OAI-PMH/oai:ListIdentifiers/oai:header')            
        result = []
        for header_node in header_nodes:
            header = buildHeader(header_node, namespaces)
            result.append(header)
        return result, token

    def buildSets(self, namespaces, tree):
        evaluator = etree.XPathEvaluator(tree, 
                                         namespaces=namespaces)
        # first find resumption token if available
        token = evaluator.evaluate(
            'string(/oai:OAI-PMH/oai:ListSets/oai:resumptionToken/text())')
        if token.strip() == '':
            token = None  
        set_nodes = evaluator.evaluate(
            '/oai:OAI-PMH/oai:ListSets/oai:set')
        sets = []
        for set_node in set_nodes:
            e = etree.XPathEvaluator(set_node, 
                                     namespaces=namespaces).evaluate
            # make sure we get back unicode strings instead
            # of lxml.etree._ElementUnicodeResult objects.
            setSpec = unicode(e('string(oai:setSpec/text())'))
            setName = unicode(e('string(oai:setName/text())'))
            # XXX setDescription nodes
            sets.append((setSpec, setName, None))
        return sets, token

    def listRecordsByDir(self, dirname, metadataPrefix):
        for dirpath, dirnames, filenames in os.walk(dirname):
            for fname in filenames:
                print fname
                full_fname = os.path.join(dirpath, fname)
                if fname.endswith('.gz'):
                    f = gzip.open(full_fname, 'r')
                else:
                    f = open(full_fname, 'r')
                tree = self.loadResponseFromFile(f)
                f.close()
                records, resumption = self.ListRecordsFromFile(metadataPrefix, tree)
                for record in records:
                    yield record

    def loadResponseFromFile(self, fdesc):
        xml = fdesc.read()
        try:
            tree = self.parse(xml)
        except SyntaxError:
            raise error.XMLSyntaxError(kw)
        return tree

    def makeRequestErrorHandling(self, **kw):
        xml = self.makeRequest(**kw)
        try:
            tree = self.parse(xml)
        except SyntaxError:
            raise error.XMLSyntaxError(kw)
        # check whether there are errors first
        e_errors = tree.xpath('/oai:OAI-PMH/oai:error',
                              namespaces=self.getNamespaces())
        if e_errors:
            # XXX right now only raise first error found, does not
            # collect error info
            for e_error in e_errors:
                code = e_error.get('code')
                msg = e_error.text
                if code not in ['badArgument', 'badResumptionToken',
                                'badVerb', 'cannotDisseminateFormat',
                                'idDoesNotExist', 'noRecordsMatch',
                                'noMetadataFormats', 'noSetHierarchy']:
                    raise error.UnknownError,\
                          "Unknown error code from server: %s, message: %s" % (
                        code, msg)
                # find exception in error module and raise with msg
                raise getattr(error, code[0].upper() + code[1:] + 'Error'), msg
        return tree
    
    def makeRequest(self, **kw):
        raise NotImplementedError
    
class Client(BaseClient):
    def __init__(
            self, base_url, metadata_registry=None, credentials=None, local_file=False):
        BaseClient.__init__(self, metadata_registry)
        self._base_url = base_url
        self._local_file = local_file
        self.get_method = False
        if credentials is not None:
            self._credentials = base64.encodestring('%s:%s' % credentials)
        else:
            self._credentials = None
            
    def makeRequest(self, **kw):
        """Either load a local XML file or actually retrieve XML from a server.
        """
        if self._local_file:
            with codecs.open(self._base_url, 'r', 'utf-8') as xmlfile:
                text = xmlfile.read()
            return text.encode('ascii', 'replace')
        else:
            # XXX include From header?
            headers = {'User-Agent': 'pyoai'}
            if self._credentials is not None:
                headers['Authorization'] = 'Basic ' + self._credentials.strip()
            kw = kw.copy()
            kw.update(self.extra_parameters)
            if self.get_method:
                request = urllib2.Request(
                        self._base_url+'?'+urlencode(kw), headers=headers)
            else:
                request = urllib2.Request(
                    self._base_url, data=urlencode(kw), headers=headers)
            return retrieveFromUrlWaiting(request)

def buildHeader(header_node, namespaces, metadata_prefix=None):
    e = etree.XPathEvaluator(header_node, 
                            namespaces=namespaces).evaluate
    identifier = e('string(oai:identifier/text())')
    datestamp = datestamp_to_datetime(
        str(e('string(oai:datestamp/text())')))
    format = e('string(oai:format/text())') or metadata_prefix
    setspec = [str(s) for s in e('oai:setSpec/text()')]
    deleted = e("@status = 'deleted'") 
    return common.Header(header_node, identifier, datestamp, setspec, deleted, format)

def ResumptionListGenerator(firstBatch, nextBatch):
    result, token = firstBatch()
    while 1:
        itemFound = False
        for item in result:
            yield item
            itemFound = True
        if token is None or not itemFound:
            break
        else:
            print "Next token: "+token
        result, token = nextBatch(token)

def retrieveFromUrlWaiting(request,
                           wait_max=WAIT_MAX, wait_default=WAIT_DEFAULT):
    """Get text from URL, handling 503 Retry-After.
    """
    for i in range(wait_max):
        try:
            f = urllib2.urlopen(request)
            text = f.read()
            f.close()
            # we successfully opened without having to wait
            break
        except urllib2.HTTPError, e:
            if e.code >= 500:
                try:
                    retryAfter = int(e.hdrs.get('Retry-After'))
                except TypeError:
                    retryAfter = None
                print "Caught 50* error. Retry after: "+str(retryAfter)
                if retryAfter is None:
                    time.sleep(wait_default)
                else:
                    time.sleep(retryAfter)
            elif e.code == 404:
                text = e.read()
                print "Caught 404 error. Text is: "+text[:200]+"..."
                # Some OAI servers give a 404 error instead of 200 with the OAI error noRecordsMatch
                if not '<OAI-PMH' in text:
                    raise e
                else:
                    return text
            else:
                # reraise any other HTTP error
                raise
        except urllib2.URLError, e:
            time.sleep(wait_default)
    else:
        raise Error, "Waited too often (more than %s times)" % wait_max
    return text

class ServerClient(BaseClient):
    def __init__(self, server, metadata_registry=None):
        BaseClient.__init__(self, metadata_registry)
        self._server = server
        
    def makeRequest(self, **kw):
        return self._server.handleRequest(kw)
