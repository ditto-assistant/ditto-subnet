import os
import sys
import json
from tqdm import tqdm
import backoff
import openai
from openai import OpenAI
import numpy as np


model_zoo = {
    'llama-3.1-70b-instruct': ('meta-llama/Meta-Llama-3.1-70B-Instruct', 'local'),
    'gpt-4o-mini': ('gpt-4o-mini-2024-07-18', 'openai'),
    'gpt-4o': ('gpt-4o-2024-08-06', 'openai'),
    # Ditto fork: the judge SN118 scores with in production. 'openai' here
    # means "an OpenAI-compatible endpoint from the environment", which is how
    # the official gpt-4o entry already reaches a proxy via OPENAI_BASE_URL.
    'deepseek-v4.1-flash': ('deepseek/deepseek-v4.1-flash', 'openai'),
}


# Ditto fork: per-judge completion bound, defaulting to upstream's 10.
#
# max_tokens=10 is correct only for a non-reasoning judge -- gpt-4o answers the
# evaluator prompt in 1-2 tokens. A reasoning judge spends the whole budget on
# reasoning tokens and returns content="" with finish_reason="length"; the
# label line below then evaluates `'yes' in ''` as False and scores EVERY
# question "no". That failure is biased rather than noisy and raises no error
# anywhere, so it reads as a model that cannot do the task at all.
#
# 8192 is set from measurement. Over the 76 probes that truncated
# deepseek-v4.1-flash at a 1024 bound, re-run at 8192 x3 replicates, the
# observed completion maximum was 2907 tokens with zero truncations. The bound
# is a cap, not a charge: mean observed completion is 62 tokens.
max_tokens_zoo = {
    'deepseek-v4.1-flash': 8192,
}


@backoff.on_exception(backoff.expo, (openai.RateLimitError,
                                    openai.APIError))
def chat_completions_with_backoff(client, **kwargs):
    return client.chat.completions.create(**kwargs)


def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response) 
    return prompt


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print('Usage: python evaluate_qa.py metric_model hyp_file ref_file')
        exit()

    metric_model_short = sys.argv[1]
    hyp_file = sys.argv[2]
    ref_file = sys.argv[3]
    verbose = True
    
    result_file = hyp_file + '.eval-results-{}'.format(metric_model_short)

    if metric_model_short not in model_zoo:
        print('Requested metric model is not supported:', metric_model_short)
        exit()
    metric_model, metric_model_source = model_zoo[metric_model_short]
    if metric_model_source == 'openai':
        openai.organization = os.getenv('OPENAI_ORGANIZATION')
        openai_api_key = os.getenv('OPENAI_API_KEY')
        openai_api_base = None
    else:
        openai_api_key = "EMPTY"
        openai_api_base = "http://localhost:8001/v1"
    
    metric_client = OpenAI(
        api_key=openai_api_key,
        base_url=openai_api_base,
    )

    try:
        hypotheses = [json.loads(line) for line in open(hyp_file).readlines()]
    except:
        hypotheses = json.load(open(hyp_file))
    try:
        references = json.load(open(ref_file))
    except:
        references = [json.loads(line) for line in open(ref_file).readlines()]
    qid2qdata = {entry['question_id']: entry for entry in references}
    qid2qtype = {entry['question_id']: entry['question_type'] for entry in references}
    qtypes = set(list(qid2qtype.values()))
    qtype2acc = {t: [] for t in qtypes}

    with open(result_file, 'w') as out_f:
        logs = []
        for entry in tqdm(hypotheses):

            if entry['question_id'] not in qid2qtype:
                print('Warning: skipping {} as it is not in reference data.'.format(entry['question_id']))
                continue
            
            qtype = qid2qtype[entry['question_id']]
            q = qid2qdata[entry['question_id']]['question']
            ans = qid2qdata[entry['question_id']]['answer']
            hyp = entry['hypothesis']
            
            prompt = get_anscheck_prompt(qtype, q, ans, hyp, abstention='_abs' in entry['question_id'])
            kwargs = {
                'model': metric_model,
                'messages':[
                    {"role": "user", "content": prompt}
                ],
                'n': 1,
                'temperature': 0,
                'max_tokens': max_tokens_zoo.get(metric_model_short, 10)
            }
            completion = chat_completions_with_backoff(metric_client, **kwargs)
            # Ditto fork: an empty completion is malformed, not a "no". Upstream
            # labels it False, which is the silent all-"no" failure described at
            # max_tokens_zoo. Matches the production runtime, which refuses the
            # same response with "official LongMemEval judge response is
            # malformed" and fails the bundle loudly.
            eval_response = (completion.choices[0].message.content or '').strip()
            if not eval_response:
                raise RuntimeError(
                    'judge returned an empty completion for {} (finish_reason={}); '
                    'raise max_tokens for this judge rather than scoring it "no"'.format(
                        entry['question_id'], completion.choices[0].finish_reason
                    )
                )
            label = 'yes' in eval_response.lower()
            entry['autoeval_label'] = {
                'model': metric_model,
                'label': label
            }
            logs.append(entry)
            if verbose:
                print(json.dumps({
                    'question': q,
                    'answer': ans,
                    'hypothesis': hyp,
                    'autoeval_label': label
                }, indent=4), flush=True)
            print(json.dumps(entry), file=out_f)
            qtype2acc[qid2qtype[entry['question_id']]].append(1 if label else 0)

            
    print('Accuracy:', round(np.mean([1 if x['autoeval_label']['label'] else 0 for x in logs]).item(), 4))
    for k,v in qtype2acc.items():
        print('\t{}: {} ({})'.format(k, round(np.mean(v), 4), len(v)))

    print('Saved to', result_file)
